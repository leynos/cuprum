"""Native-pump cleanup tracing at the cancellation boundary."""

from __future__ import annotations

import asyncio
import logging
import types
import typing as typ

import pytest

from cuprum import _pipeline_stream_cleanup_observation, _pipeline_streams
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook
from cuprum.events import new_exec_id
from cuprum.pump_observation import _correlate_pump_events, observe_pump
from cuprum.unittests._adapter_test_support import _make_exec_event
from cuprum.unittests._pipeline_wait_support import make_stage_observations

if typ.TYPE_CHECKING:
    from cuprum.pump_events import PumpEvent


def test_native_pump_cleanup_uses_the_injected_monotonic_clock(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cleanup tracing records the duration supplied by the injected clock."""
    clock_values = iter((10.0, 12.5))
    expected_duration_s = 2.5
    pump_events: list[PumpEvent] = []
    tracer = InMemoryTracer()
    tracing_hook = TracingHook(tracer)
    exec_id = new_exec_id()
    tracing_hook(_make_exec_event(phase="start", overrides={"exec_id": exec_id}))

    def monotonic_clock() -> float:
        """Return the next controlled cleanup timestamp."""
        return next(clock_values)

    async def await_completed_cleanup() -> None:
        """Run the cleanup boundary with an already-settled worker future."""
        cleanup_complete = asyncio.get_running_loop().create_future()
        cleanup_complete.set_result(None)
        await _pipeline_streams._await_native_pump_cleanup(
            cleanup_complete,
            monotonic_clock=monotonic_clock,
        )

    caplog.set_level(logging.DEBUG, logger=_pipeline_streams.__name__)
    with (
        _correlate_pump_events(exec_id),
        observe_pump(pump_events.append),
        observe_pump(tracing_hook.record_pump_event),
    ):
        asyncio.run(await_completed_cleanup())

    completed_event = pump_events[-1]
    assert completed_event.phase == "cleanup_completed", (
        f"the final cleanup event must report completion, found {pump_events}"
    )
    assert completed_event.duration_s == pytest.approx(expected_duration_s), (
        "the completion event must use the injected monotonic duration, found "
        f"{completed_event.duration_s!r}"
    )
    completed_records = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_outcome") == "completed"
    ]
    assert len(completed_records) == 1, (
        f"cleanup must emit one completed DEBUG record, found {completed_records}"
    )
    assert completed_records[0].__dict__.get("cuprum_duration_s") == pytest.approx(
        expected_duration_s
    ), (
        "the completed DEBUG record must use the injected monotonic duration, "
        f"found {completed_records[0].__dict__.get('cuprum_duration_s')!r}"
    )
    assert [event.exec_id for event in pump_events] == [exec_id, exec_id], (
        "cleanup PumpEvents must retain their source stage token for tracing"
    )
    span = tracer.spans[0]
    assert span.events == [
        (
            "cuprum.native_pump_cleanup_started",
            {"operation": "native_pump_cleanup", "outcome": "started"},
        ),
        (
            "cuprum.native_pump_cleanup_completed",
            {
                "operation": "native_pump_cleanup",
                "outcome": "completed",
                "duration_s": expected_duration_s,
            },
        ),
    ], f"cleanup tracing must use the injected duration, found {span.events!r}"
    assert span.ended is False, "cleanup tracing must not end the execution span"
    assert span.status_ok is None, "cleanup tracing must not mark the execution span"


def test_pipe_task_carries_its_source_stage_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pump task inherits the upstream stage token when it is created."""
    events: list[PumpEvent] = []
    observations = make_stage_observations(2, ())
    processes = typ.cast(
        "list[asyncio.subprocess.Process]",
        [
            types.SimpleNamespace(stdout=None, stdin=None),
            types.SimpleNamespace(stdout=None, stdin=None),
        ],
    )

    async def fake_dispatch(
        reader: asyncio.StreamReader | None,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Emit cleanup facts from the task's inherited context."""
        del reader, writer
        await asyncio.sleep(0)
        _pipeline_stream_cleanup_observation._log_native_pump_cleanup_started(
            logging.getLogger(__name__)
        )
        _pipeline_stream_cleanup_observation._log_native_pump_cleanup_completed(
            logging.getLogger(__name__),
            0.25,
        )

    async def run_pipe_task() -> None:
        """Create and await the context-correlated source-to-destination hop."""
        tasks = _pipeline_streams._create_pipe_tasks(processes, observations)
        await asyncio.gather(*tasks)

    monkeypatch.setattr(_pipeline_streams, "_pump_stream_dispatch", fake_dispatch)
    with observe_pump(events.append):
        asyncio.run(run_pipe_task())

    assert [event.exec_id for event in events] == [
        observations[0].exec_id,
        observations[0].exec_id,
    ], f"cleanup events must retain the source token, found {events!r}"
