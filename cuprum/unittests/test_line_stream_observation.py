"""Focused observability contracts for ``SafeCmd.lines()`` lifecycle events."""

from __future__ import annotations

import asyncio
import logging
import typing as typ
from collections import Counter

import pytest

from cuprum import RunOutputOptions, observe, observe_line_stream
from cuprum.adapters.line_stream_metrics import (
    LINE_STREAM_EVENTS_TOTAL,
    LineStreamMetricsHook,
)
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook
from cuprum.events import new_exec_id
from cuprum.line_stream_events import (
    LineStreamEvent,
    LineStreamPhase,
    LineStreamSink,
)
from cuprum.line_stream_observation import _emit_line_stream_event
from cuprum.lines import LineEvent
from cuprum.sh import TimeoutExpired
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecId
    from cuprum.line_stream_events import LineStreamHook
    from cuprum.lines import LineStreamName
    from cuprum.sh import SafeCmd


class _RecordingCollector:
    """Metrics collector double retaining labels for cardinality assertions."""

    def __init__(self) -> None:
        """Start with no metric calls."""
        self.counters: list[tuple[str, dict[str, str]]] = []

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record one counter increment."""
        assert value == pytest.approx(1.0), (
            "line-stream lifecycle counters increment by one"
        )
        self.counters.append((name, dict(labels)))

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Reject histograms; this adapter emits lifecycle counters only."""
        del name, value, labels
        pytest.fail("line-stream lifecycle adapter must not emit histograms")


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a safe builder for the current Python interpreter."""
    return build_python_builder()


def _collect_lines(
    command: SafeCmd,
    *,
    output: RunOutputOptions | None = None,
) -> list[LineEvent]:
    """Run one line stream to completion and retain every yielded event."""

    async def collect() -> list[LineEvent]:
        """Iterate the supplied command's line stream."""
        return [event async for event in command.lines(output=output)]

    return asyncio.run(collect())


def _assert_line_stream_output(lines: cabc.Sequence[LineEvent]) -> None:
    """Assert that a line-only stream yields exactly the expected output."""
    assert Counter((event.stream, event.text) for event in lines) == Counter({
        ("stdout", "out"): 1,
        ("stderr", "err"): 1,
    }), f"line-only iteration must retain both streams, got {lines!r}"


def _assert_lifecycle_correlation(
    lifecycle: cabc.Sequence[LineStreamEvent],
    execution: cabc.Sequence[ExecEvent],
) -> ExecId:
    """Assert that lifecycle events retain one execution correlation."""
    assert [event.phase for event in lifecycle] == ["spawned", "completed"], (
        f"successful line streams must report start and completion, got {lifecycle!r}"
    )
    exec_ids = {event.exec_id for event in execution}
    assert len(exec_ids) == 1, (
        f"line lifecycle must correlate to one execution, got {execution!r}"
    )
    exec_id = next(iter(exec_ids))
    assert exec_id is not None, (
        f"line lifecycle must retain a concrete execution ID, got {execution!r}"
    )
    assert lifecycle[0].exec_id == exec_id, (
        f"line lifecycle must retain the execution correlation, got {lifecycle!r}"
    )
    assert all(event.pid is not None for event in lifecycle), (
        f"spawned processes must carry their PID, got {lifecycle!r}"
    )
    return exec_id


def _assert_structured_lifecycle_logs(
    records: cabc.Iterable[logging.LogRecord],
    exec_id: ExecId,
) -> None:
    """Assert structured logs preserve lifecycle order and correlation."""
    structured_records = [
        record
        for record in records
        if record.__dict__.get("cuprum_action") == "line_stream_event"
    ]
    assert [record.__dict__["cuprum_phase"] for record in structured_records] == [
        "spawned",
        "completed",
    ], f"structured logs must preserve lifecycle phases, got {structured_records!r}"
    assert all(
        record.__dict__["cuprum_exec_id"] == exec_id for record in structured_records
    ), (
        "structured logs must retain the execution correlation, got "
        f"{structured_records!r}"
    )


def _assert_traced_lifecycle_events(tracer: InMemoryTracer) -> None:
    """Assert tracing records exactly the line-stream lifecycle boundaries."""
    assert len(tracer.spans) == 1, (
        f"one command must create one span, got {tracer.spans!r}"
    )
    assert [
        name for name, _attrs in tracer.spans[0].events if name == "cuprum.line_stream"
    ] == [
        "cuprum.line_stream",
        "cuprum.line_stream",
    ], f"tracing must receive both lifecycle boundaries, got {tracer.spans[0].events!r}"


def test_line_stream_lifecycle_is_correlated_logged_and_traced(
    caplog: pytest.LogCaptureFixture,
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A successful stream reports correlated boundaries through every adapter."""
    command = python_builder(
        "-c", "import sys; print('out'); print('err', file=sys.stderr)"
    )
    lifecycle: list[LineStreamEvent] = []
    execution: list[ExecEvent] = []
    tracer = InMemoryTracer()
    tracing_hook = TracingHook(tracer)

    with (
        caplog.at_level(logging.DEBUG, logger="cuprum.line_stream_observation"),
        observe(execution.append),
        observe(tracing_hook),
        observe_line_stream(lifecycle.append),
        observe_line_stream(tracing_hook.record_line_stream_event),
    ):
        lines = _collect_lines(
            command, output=RunOutputOptions(capture=False, echo=False)
        )

    _assert_line_stream_output(lines)
    exec_id = _assert_lifecycle_correlation(lifecycle, execution)
    _assert_structured_lifecycle_logs(caplog.records, exec_id)
    _assert_traced_lifecycle_events(tracer)


def test_queue_saturation_reports_bounded_queue_details() -> None:
    """The queue reports saturation once with size and capacity as event data."""
    from cuprum._line_stream import (
        _line_event_queue,
        _LineStreamTelemetry,
        _queue_line_sink,
    )

    seen: list[LineStreamEvent] = []

    async def exercise() -> None:
        """Fill a one-item queue and release one parked line sink."""
        queue = _line_event_queue()
        telemetry = _LineStreamTelemetry(
            exec_id=new_exec_id(),
            queue_capacity=queue.maxsize,
            pid=123,
        )
        sink = _queue_line_sink(queue, telemetry)
        for index in range(queue.maxsize):
            queue.put_nowait(LineEvent(stream="stdout", at=0.0, text=str(index)))
        outcome = sink(LineEvent(stream="stderr", at=0.0, text="second"))
        assert outcome is not None, "a full queue must return an awaitable sink"
        pending = asyncio.ensure_future(outcome)
        await asyncio.sleep(0)
        queue.get_nowait()
        await pending
        assert telemetry.is_queue_saturated, (
            "the state remains saturated when the parked line refills the queue"
        )

    with observe_line_stream(seen.append):
        asyncio.run(exercise())

    assert [(event.phase, event.stream) for event in seen] == [
        ("queue_saturated", "stderr"),
    ], f"queue saturation must report its blocked stream, got {seen!r}"
    queue_size = seen[0].queue_size
    queue_capacity = seen[0].queue_capacity
    assert queue_size is not None, (
        f"saturation must carry bounded queue details, got {seen[0]!r}"
    )
    assert queue_capacity is not None, (
        f"saturation must carry its queue capacity, got {seen[0]!r}"
    )
    assert queue_size == queue_capacity, (
        f"saturation must report its finite bound, got {seen[0]!r}"
    )
    assert queue_size > 0, (
        f"saturation must report its positive finite bound, got {seen[0]!r}"
    )


def test_line_stream_reports_callback_failure_and_teardown(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Callback failures remain command failures while lifecycle telemetry survives."""
    command = python_builder("-c", "print('callback')")
    seen: list[LineStreamEvent] = []

    def fail_callback(_event: LineEvent) -> None:
        """Model a caller callback failure."""
        msg = "callback failure"
        raise ValueError(msg)

    async def collect() -> None:
        """Iterate until the caller callback fails."""
        async for _event in command.lines(
            output=RunOutputOptions(on_line=fail_callback),
        ):
            pass

    with (
        observe_line_stream(seen.append),
        pytest.raises(ValueError, match="callback failure"),
    ):
        asyncio.run(collect())

    failed = [event for event in seen if event.phase == "sink_failed"]
    assert [(event.sink, event.error_type) for event in failed] == [
        ("callback", "ValueError"),
    ], f"callback failures must retain their bounded source, got {seen!r}"
    assert {"teardown_started", "teardown_completed"} <= {
        event.phase for event in seen
    }, f"callback failure must still reconcile consumers, got {seen!r}"


def test_line_stream_timeout_and_close_report_their_boundaries(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Timeout and iterator close produce distinct lifecycle boundaries."""
    timeout_events: list[LineStreamEvent] = []
    timeout_command = python_builder("-c", "import time; time.sleep(5)")

    async def time_out() -> None:
        """Consume a stream whose child exceeds its deadline."""
        async for _event in timeout_command.lines(timeout=0.01):
            pass

    with observe_line_stream(timeout_events.append), pytest.raises(TimeoutExpired):
        asyncio.run(time_out())

    assert {"timeout", "teardown_started", "teardown_completed"} <= {
        event.phase for event in timeout_events
    }, f"timeout must report its lifecycle boundaries, got {timeout_events!r}"

    cancellation_events: list[LineStreamEvent] = []
    close_command = python_builder(
        "-c",
        "import time; print('ready', flush=True); time.sleep(5)",
    )

    async def close_started_stream() -> None:
        """Start one stream, receive its ready line, and explicitly close it."""
        stream = close_command.lines()
        await anext(stream)
        await stream.aclose()

    with observe_line_stream(cancellation_events.append):
        asyncio.run(close_started_stream())

    assert {"cancelled", "teardown_started", "teardown_completed"} <= {
        event.phase for event in cancellation_events
    }, (
        "closing a started stream must report cancellation teardown, got "
        f"{cancellation_events!r}"
    )


def test_metrics_labels_stay_bounded_for_publicly_constructed_events() -> None:
    """Metrics never turn identifiers, errors, or queue details into labels."""
    collector = _RecordingCollector()
    event = LineStreamEvent(
        phase=typ.cast("LineStreamPhase", "unbounded-phase"),
        exec_id=new_exec_id(),
        pid=98765,
        stream=typ.cast("LineStreamName", "unbounded-stream"),
        sink=typ.cast("LineStreamSink", "unbounded-sink"),
        queue_size=999,
        queue_capacity=1000,
        error_type="UnboundedErrorName",
    )

    LineStreamMetricsHook(collector)(event)

    assert collector.counters == [
        (
            LINE_STREAM_EVENTS_TOTAL,
            {"phase": "unknown", "stream": "unknown", "sink": "unknown"},
        ),
    ], (
        "metrics labels must be drawn from fixed vocabularies, got "
        f"{collector.counters!r}"
    )


def test_metrics_preserve_each_valid_sink_label() -> None:
    """Metrics retain both values from the closed line-sink vocabulary."""
    collector = _RecordingCollector()

    for sink in typ.get_args(LineStreamSink.__value__):
        LineStreamMetricsHook(collector)(
            LineStreamEvent(
                phase=LineStreamPhase.SPAWNED,
                exec_id=new_exec_id(),
                pid=1,
                sink=sink,
            )
        )

    assert [labels["sink"] for _name, labels in collector.counters] == [
        "callback",
        "queue",
    ], f"metrics must preserve valid sink labels, got {collector.counters!r}"


def test_telemetry_observer_failure_is_logged_without_masking_delivery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken lifecycle observer is logged and does not stop later observers."""
    seen: list[LineStreamEvent] = []
    event = LineStreamEvent(
        phase=LineStreamPhase.SPAWNED,
        exec_id=new_exec_id(),
        pid=1,
    )

    def fail(_event: LineStreamEvent) -> None:
        """Raise as a broken telemetry backend would."""
        msg = "collector failure"
        raise RuntimeError(msg)

    with (
        caplog.at_level(logging.WARNING, logger="cuprum.line_stream_observation"),
        observe_line_stream(typ.cast("LineStreamHook", fail)),
        observe_line_stream(seen.append),
    ):
        _emit_line_stream_event(event)

    assert seen == [event], "a later observer must still receive the event"
    failures = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "line_stream_observer_failed"
    ]
    assert len(failures) == 1, (
        f"observer failure must be logged with its traceback, got {failures!r}"
    )
    assert failures[0].exc_info is not None, (
        f"observer failure must retain its traceback, got {failures!r}"
    )
