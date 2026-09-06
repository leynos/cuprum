"""Production-path tests for aggregate Python stream-operation observation."""

from __future__ import annotations

import asyncio
import io
import typing as typ

from cuprum import stream_observation
from cuprum._streams import _drain, _StreamConfig
from cuprum._streams_pump import _pump_stream
from cuprum.adapters.stream_metrics import (
    STREAM_OPERATION_BYTES_TOTAL,
    STREAM_OPERATION_DURATION_SECONDS,
    STREAM_OPERATION_READ_OPERATIONS_TOTAL,
    stream_operation_metrics_hook,
)
from cuprum.events import new_exec_id
from cuprum.pump_observation import _correlate_pump_events
from cuprum.stream_events import StreamOperation, StreamOperationOutcome
from cuprum.stream_observation import observe_stream_operation

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest


class _Reader:
    """Deterministic reader that returns queued chunks followed by EOF."""

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        """Store chunks for later reads."""
        self._chunks = list(chunks)
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        """Return the next chunk or EOF."""
        self.read_sizes.append(size)
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _StallingReader:
    """Reader that yields one chunk before waiting indefinitely."""

    def __init__(self, first_chunk: bytes) -> None:
        """Store the sole chunk that arrives before the stall."""
        self._first_chunk = first_chunk
        self._read_calls = 0

    async def read(self, _: int) -> bytes:
        """Yield once, then wait until the bounded drain cancels the read."""
        self._read_calls += 1
        if self._read_calls == 1:
            return self._first_chunk
        await asyncio.sleep(3600)
        return b"unreachable"


class _Writer:
    """Deterministic writer that can close on a chosen drain call."""

    def __init__(self, *, fail_on_drain_call: int | None = None) -> None:
        """Configure the optional downstream-close point."""
        self.data = bytearray()
        self._drain_calls = 0
        self._fail_on_drain_call = fail_on_drain_call

    def write(self, chunk: bytes) -> None:
        """Record bytes accepted before the downstream close."""
        self.data.extend(chunk)

    async def drain(self) -> None:
        """Optionally simulate a downstream broken pipe."""
        self._drain_calls += 1
        if self._drain_calls == self._fail_on_drain_call:
            raise BrokenPipeError

    def write_eof(self) -> None:
        """Accept writer EOF during normal cleanup."""

    def close(self) -> None:
        """Accept writer closure during normal cleanup."""

    async def wait_closed(self) -> None:
        """Accept the awaited writer cleanup."""


class _MetricRecorder:
    """Collector double retaining metric names, values, and labels."""

    def __init__(self) -> None:
        """Initialize an empty operation log."""
        self.calls: list[tuple[str, float, dict[str, str]]] = []

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record one counter operation."""
        self.calls.append((name, value, dict(labels)))

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record one histogram operation."""
        self.calls.append((name, value, dict(labels)))


def _drain_config() -> _StreamConfig:
    """Build a capture-only configuration for direct drain tests."""
    return _StreamConfig(
        capture_output=True,
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="replace",
    )


def test_drain_emits_exact_aggregate_completion() -> None:
    """A completed drain reports its bytes, reads, and monotonic duration."""
    seen = []

    with observe_stream_operation(seen.append):
        captured = asyncio.run(
            _drain(
                typ.cast("asyncio.StreamReader", _Reader((b"abc", b"de"))),
                _drain_config(),
            )
        )

    assert captured == "abcde", "drain must preserve captured payload"
    assert len(seen) == 1, "one completed drain must emit exactly one event"
    event = seen[0]
    assert event.operation is StreamOperation.DRAIN, "event must identify draining"
    assert event.outcome is StreamOperationOutcome.EOF, "drain must report EOF"
    assert event.bytes_consumed == 5, "event must count all returned payload bytes"
    assert event.read_operations == 3, "event must count chunks and EOF"
    assert event.duration_s >= 0, "duration must use a monotonic clock"


def test_pump_emits_exact_aggregate_completion() -> None:
    """A completed pipeline transfer reports its bytes, reads, and duration."""
    seen = []
    reader = _Reader((b"abc", b"de"))
    writer = _Writer()

    with observe_stream_operation(seen.append):
        asyncio.run(
            _pump_stream(
                typ.cast("asyncio.StreamReader", reader),
                typ.cast("asyncio.StreamWriter", writer),
            )
        )

    assert writer.data == b"abcde", "pump must preserve written payload"
    assert len(seen) == 1, "one completed pump must emit exactly one event"
    event = seen[0]
    assert event.operation is StreamOperation.PIPELINE_TRANSFER, (
        "event must identify pipeline transfer"
    )
    assert event.outcome is StreamOperationOutcome.EOF, "pump must report EOF"
    assert event.bytes_consumed == 5, "event must count all returned payload bytes"
    assert event.read_operations == 3, "event must count chunks and EOF"
    assert event.duration_s >= 0, "duration must use a monotonic clock"


def test_pump_uses_existing_pipeline_execution_correlation() -> None:
    """A pump event inherits only the existing upstream-stage token."""
    seen = []
    exec_id = new_exec_id()

    with observe_stream_operation(seen.append), _correlate_pump_events(exec_id):
        asyncio.run(
            _pump_stream(
                typ.cast("asyncio.StreamReader", _Reader((b"payload",))),
                typ.cast("asyncio.StreamWriter", _Writer()),
            )
        )

    assert seen[0].exec_id == exec_id, "pump must retain existing stage correlation"


def test_pump_reports_post_close_bytes_after_downstream_closure() -> None:
    """A downstream closure records bytes consumed by the bounded drain."""
    seen = []
    reader = _Reader((b"first", b"discarded", b"later"))
    writer = _Writer(fail_on_drain_call=1)

    with observe_stream_operation(seen.append):
        asyncio.run(
            _pump_stream(
                typ.cast("asyncio.StreamReader", reader),
                typ.cast("asyncio.StreamWriter", writer),
            )
        )

    event = seen[0]
    assert event.outcome is StreamOperationOutcome.DOWNSTREAM_CLOSED, (
        "completed bounded drain must retain the downstream-close outcome"
    )
    assert event.bytes_consumed == 19, "event must include post-close discarded bytes"
    assert event.read_operations == 4, "event must include the bounded drain EOF"


def test_pump_reports_partial_total_when_bounded_post_close_drain_times_out() -> None:
    """A bounded drain timeout preserves bytes consumed before its stalled read."""
    seen = []
    reader = _StallingReader(b"partial")
    writer = _Writer(fail_on_drain_call=1)

    with observe_stream_operation(seen.append):
        asyncio.run(
            _pump_stream(
                typ.cast("asyncio.StreamReader", reader),
                typ.cast("asyncio.StreamWriter", writer),
            )
        )

    event = seen[0]
    assert event.outcome is StreamOperationOutcome.POST_CLOSE_DRAIN_TIMEOUT, (
        "timed-out bounded drain must report its closed outcome"
    )
    assert event.bytes_consumed == 7, "event must retain bytes before timeout"
    assert event.read_operations == 1, "cancelled reads must not count as completed"


def test_unobserved_operations_do_not_emit_events_or_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unregistered stream operations leave an unregistered collector untouched."""
    collector = _MetricRecorder()

    def unexpected_emit(*_: object, **__: object) -> None:
        """Fail if an unregistered operation attempts to emit an event."""
        msg = "unregistered operations must not emit"
        raise AssertionError(msg)

    monkeypatch.setattr(
        stream_observation,
        "_emit_stream_operation_event",
        unexpected_emit,
    )

    captured = asyncio.run(
        _drain(
            typ.cast("asyncio.StreamReader", _Reader((b"payload",))), _drain_config()
        )
    )
    writer = _Writer()
    asyncio.run(
        _pump_stream(
            typ.cast("asyncio.StreamReader", _Reader((b"pipeline",))),
            typ.cast("asyncio.StreamWriter", writer),
        )
    )

    assert captured == "payload", "unobserved drain must still capture output"
    assert writer.data == b"pipeline", "unobserved pump must still transfer output"
    assert collector.calls == [], "no registration must produce no metric calls"


def test_failing_observer_does_not_change_drain_or_pump_success() -> None:
    """A failing aggregate observer cannot replace successful stream results."""

    def fail(_: object) -> None:
        """Raise from the observer to exercise the fail-open boundary."""
        msg = "observer failure"
        raise RuntimeError(msg)

    with observe_stream_operation(fail):
        captured = asyncio.run(
            _drain(
                typ.cast("asyncio.StreamReader", _Reader((b"drain",))), _drain_config()
            )
        )
        writer = _Writer()
        asyncio.run(
            _pump_stream(
                typ.cast("asyncio.StreamReader", _Reader((b"pump",))),
                typ.cast("asyncio.StreamWriter", writer),
            )
        )

    assert captured == "drain", "failing observer must not change drain output"
    assert writer.data == b"pump", "failing observer must not change pump output"


def test_stream_metric_labels_are_closed() -> None:
    """Aggregate metrics use only the documented operation and outcome labels."""
    collector = _MetricRecorder()

    with observe_stream_operation(stream_operation_metrics_hook(collector)):
        asyncio.run(
            _drain(
                typ.cast("asyncio.StreamReader", _Reader((b"payload",))),
                _drain_config(),
            )
        )

    assert len(collector.calls) == 3, (
        "completed stream operation must yield three metrics"
    )
    assert {call[0] for call in collector.calls} == {
        STREAM_OPERATION_BYTES_TOTAL,
        STREAM_OPERATION_READ_OPERATIONS_TOTAL,
        STREAM_OPERATION_DURATION_SECONDS,
    }, "stream metrics must retain their documented names"
    labels = {tuple(sorted(call[2].items())) for call in collector.calls}
    assert labels == {(("operation", "stream_drain"), ("outcome", "eof"))}, (
        "stream metrics must expose only the documented closed labels"
    )
