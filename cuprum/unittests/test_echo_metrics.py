"""Metrics for stream-echo encoding failures, driven from the real drain path.

Every test here drives ``_drain`` with a sink that genuinely raises
``UnicodeEncodeError``, so removing the emission from the echo guard fails
these tests rather than leaving them green against a stub.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._streams import _drain, _StreamConfig
from cuprum.adapters.echo_metrics import (
    ECHO_ENCODING_FAILURES_TOTAL,
    EchoMetricsHook,
    echo_metrics_hook,
)
from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream
from cuprum.echo_observation import observe_echo
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.adapters.metrics_adapter import MetricsCollector


class _Cp1252TextOnlySink:
    """Text-only sink modelling a parent stream too narrow for the output."""

    def write(self, payload: str) -> int:
        """Reject payloads the CP1252 codec cannot represent."""
        payload.encode("cp1252")
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


def _drain_config(
    sink: typ.IO[str],
    stream: EchoStream,
) -> _StreamConfig:
    """Build a UTF-8 config for one named output stream."""
    return _StreamConfig(
        capture_output=True,
        echo_output=True,
        sink=sink,
        encoding="utf-8",
        errors="replace",
        stream=stream,
    )


def _run_drain(stream: EchoStream) -> None:
    """Run one drain whose echo path hits the sink's encoding limit."""
    sink = typ.cast("typ.IO[str]", _Cp1252TextOnlySink())
    chunks = (b"plain ", "ś".encode(), b" plain ", "ń".encode())
    asyncio.run(_drain(_echo_reader(chunks), _drain_config(sink, stream)))


class _ChunkedReader:
    """Stub stream reader yielding queued chunks before EOF."""

    def __init__(self, chunks: cabc.Sequence[bytes]) -> None:
        """Store chunks for sequential ``read`` calls."""
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        """Return the next queued chunk, or empty bytes at EOF."""
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _echo_reader(chunks: cabc.Sequence[bytes]) -> asyncio.StreamReader:
    """Build a stream-reader-shaped stub for the given chunks."""
    return typ.cast("asyncio.StreamReader", _ChunkedReader(chunks))


def test_stdout_failure_increments_only_the_stdout_series() -> None:
    """A stdout drain failure counts once under ``stream=stdout``."""
    collector = RecordingCollector()

    with observe_echo(EchoMetricsHook(collector)):
        _run_drain(EchoStream.STDOUT)

    assert len(collector.counters) == 1, (
        "a stdout failure must increment exactly one counter, "
        f"found {collector.counters}"
    )
    name, value, labels = collector.counters[0]
    assert name == ECHO_ENCODING_FAILURES_TOTAL
    assert value == 1.0  # ruff: ignore[float-equality-comparison] - exact increment
    assert labels == {"stream": "stdout", "error_category": "unicode_encode"}


def test_stderr_failure_increments_only_the_stderr_series() -> None:
    """A stderr drain failure counts once under ``stream=stderr``."""
    collector = RecordingCollector()

    with observe_echo(EchoMetricsHook(collector)):
        _run_drain(EchoStream.STDERR)

    assert len(collector.counters) == 1, (
        "a stderr failure must increment exactly one counter, "
        f"found {collector.counters}"
    )
    name, value, labels = collector.counters[0]
    assert name == ECHO_ENCODING_FAILURES_TOTAL
    assert value == 1.0  # ruff: ignore[float-equality-comparison] - exact increment
    assert labels == {"stream": "stderr", "error_category": "unicode_encode"}


def test_independent_streams_produce_one_increment_each() -> None:
    """Stdout and stderr failures count separately under their own labels."""
    collector = RecordingCollector()

    with observe_echo(EchoMetricsHook(collector)):
        _run_drain(EchoStream.STDOUT)
        _run_drain(EchoStream.STDERR)

    assert len(collector.counters) == 2, (
        "one failure per stream must produce two increments, "
        f"found {collector.counters}"
    )
    streams = [labels["stream"] for _name, _value, labels in collector.counters]
    assert streams == ["stdout", "stderr"], (
        f"each stream must be labelled once, found streams={streams!r}"
    )


def test_repeated_chunks_after_disable_emit_no_second_increment() -> None:
    """The guard's disabled state suppresses both the warning and the counter."""
    collector = RecordingCollector()

    with observe_echo(EchoMetricsHook(collector)):
        sink = typ.cast("typ.IO[str]", _Cp1252TextOnlySink())
        chunks = (b"plain ", "ś".encode(), b" plain ", "ń".encode())
        asyncio.run(
            _drain(_echo_reader(chunks), _drain_config(sink, EchoStream.STDOUT)),
        )

    assert len(collector.counters) == 1, (
        f"repeat chunks must not compound the counter, found {collector.counters}"
    )


def test_metric_labels_carry_no_unbounded_values() -> None:
    """The label set is exactly the two bounded keys, with no sink metadata."""
    collector = RecordingCollector()

    with observe_echo(EchoMetricsHook(collector)):
        _run_drain(EchoStream.STDOUT)

    _name, _value, labels = collector.counters[0]
    assert set(labels) == {"stream", "error_category"}, (
        f"labels must stay bounded, found {sorted(labels)!r}"
    )
    assert labels["stream"] in {"stdout", "stderr"}
    assert labels["error_category"] == EchoErrorCategory.UNICODE_ENCODE.value


def test_hook_factory_returns_the_hook_type() -> None:
    """The convenience factory returns a working echo hook."""
    collector = RecordingCollector()
    hook = echo_metrics_hook(collector)

    hook(
        EchoEvent(
            stream=EchoStream.STDOUT,
            error_category=EchoErrorCategory.UNICODE_ENCODE,
        ),
    )

    assert len(collector.counters) == 1


def test_failing_collector_does_not_break_the_drain() -> None:
    """A raising collector cannot change the drain's capture contract."""

    class _ExplodingCollector:
        """Metrics collector that fails on every call."""

        def inc_counter(
            self,
            _name: str,
            _value: float,
            _labels: cabc.Mapping[str, str],
        ) -> None:
            """Model an unreachable metrics backend."""
            msg = "collector exploded"
            raise RuntimeError(msg)

        def observe_histogram(
            self,
            _name: str,
            _value: float,
            _labels: cabc.Mapping[str, str],
        ) -> None:
            """Unused by the echo hook."""

    sink = typ.cast("typ.IO[str]", _Cp1252TextOnlySink())
    chunks = (b"plain ", "ś".encode(), b" plain ", "ń".encode())

    with observe_echo(
        EchoMetricsHook(typ.cast("MetricsCollector", _ExplodingCollector())),
    ):
        captured = asyncio.run(
            _drain(_echo_reader(chunks), _drain_config(sink, EchoStream.STDOUT)),
        )

    assert captured == "plain ś plain ń", (
        f"capture must survive a broken collector, found captured={captured!r}"
    )
