"""Recorder seams: the retention sink port and the injected clock.

The recorder publishes one bounded sample per retention decision. These tests
pin the two seams that carry those samples out of the recorder: how a stage
discovers a hook that implements the sink port, and how the injectable clock
makes a capacity wait's duration exact and observable even when the wait is
cancelled. The log records themselves are pinned in
``test_observe_task_retention_telemetry.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from cuprum._observability import _wait_for_exec_hook_tasks
from cuprum._observe_task_retention import (
    _OBSERVE_TASK_CEILING,
    _await_hook_capacity,
)
from cuprum._pipeline_types import _EventDetails
from cuprum._retention_telemetry import (
    _RETENTION_CAPACITY_WAIT,
    _RETENTION_DEPTH,
    _RETENTION_PRUNE,
    _RETENTION_SATURATION,
    _RetentionSample,
    _RetentionTelemetry,
)
from cuprum.events import new_exec_id
from cuprum.unittests._rust_pump_test_helpers import ControllableMonotonicClock
from cuprum.unittests.test_line_event_emission_support import _make_observation
from cuprum.unittests.test_observe_task_retention import (
    _hold,
    _HookFailureError,
)
from cuprum.unittests.test_observe_task_retention_callbacks import _ExplodingHook

#: Every phase label these tests use is a closed event or stream name, so the
#: sample's phase is a bounded label rather than free text.
_STDOUT = "stdout"


class _RecordingSink:
    """Capture every sample a recorder publishes, as an observe hook."""

    def __init__(self) -> None:
        """Start with no recorded samples."""
        self.samples: list[_RetentionSample] = []

    def __call__(self, event: object) -> None:
        """Accept an execution event and record nothing about it."""

    def report_retention(self, sample: _RetentionSample) -> None:
        """Record one published retention sample."""
        self.samples.append(sample)

    def kinds(self) -> list[str]:
        """Return the kinds of the recorded samples, in publication order."""
        return [sample.kind for sample in self.samples]


class _SinkError(RuntimeError):
    """Failure raised by a retention projection that cannot be exported."""

    def __init__(self) -> None:
        """Build the failure with its fixed message."""
        super().__init__("projection failed")


class _RaisingSink:
    """Observe hook whose retention projection always fails."""

    def __call__(self, event: object) -> None:
        """Accept an execution event and project nothing about it."""

    def report_retention(self, sample: _RetentionSample) -> None:
        """Fail, as a broken exporter would.

        Raises
        ------
        _SinkError
            Always, so a caller can observe how the emission handles it.
        """
        raise _SinkError


async def _immediate() -> None:
    """Complete without doing any work."""


async def _settle_to_ceiling(pending_tasks: list[asyncio.Task[None]]) -> None:
    """Fill the shared retention list to the ceiling with settled tasks.

    Retention only runs at the ceiling, so a projection can only fail during
    retention once the list is full.
    """
    tasks = [asyncio.create_task(_immediate()) for _ in range(_OBSERVE_TASK_CEILING)]
    pending_tasks.extend(tasks)
    await asyncio.gather(*tasks)


def test_raising_sink_does_not_mask_a_hook_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The hook's own error must outrank a failure in the retention sink."""

    async def case() -> None:
        """Emit a failing hook's event with the list at the ceiling."""
        observation = _make_observation((_ExplodingHook(), _RaisingSink()))
        await _settle_to_ceiling(observation.pending_tasks)

        with pytest.raises(_HookFailureError):
            observation.emit("stdout", _EventDetails(pid=1, line="a line"))

        await _wait_for_exec_hook_tasks(observation.pending_tasks)

    asyncio.run(case())

    assert "observe_task_retention_failed" in caplog.text, (
        "the swallowed sink failure must be logged rather than hidden"
    )


def test_raising_sink_fails_the_emission_it_serves() -> None:
    """With no earlier error, a failing projection must surface to the caller."""

    async def case() -> None:
        """Emit with the list at the ceiling and a failing sink installed."""
        observation = _make_observation((_RaisingSink(),))
        await _settle_to_ceiling(observation.pending_tasks)

        with pytest.raises(_SinkError):
            observation.emit("stdout", _EventDetails(pid=1, line="a line"))

    asyncio.run(case())


def test_stage_observation_discovers_a_retention_sink() -> None:
    """A hook implementing the sink port must be installed on the recorder."""
    sink = _RecordingSink()
    observation = _make_observation(observe_hooks=(sink,))

    assert observation.retention_telemetry.sinks == (sink,), (
        "the stage must discover the retention sink among its observe hooks"
    )


def test_capacity_wait_duration_uses_the_injected_clock() -> None:
    """A controlled clock must make the recorded wait duration exact."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        clock = ControllableMonotonicClock()
        recorder = _RetentionTelemetry(exec_id=new_exec_id(), clock=clock)
        pending = [asyncio.create_task(_hold(gate))]
        await asyncio.sleep(0)

        waiter = asyncio.create_task(
            _await_hook_capacity(pending, 1, telemetry=recorder, phase=_STDOUT)
        )
        await asyncio.sleep(0)
        clock.advance(2.5)
        gate.set()
        await asyncio.wait_for(waiter, timeout=5.0)

        assert recorder.waits == 1, "one suspended line must be one wait"
        assert recorder.wait_seconds == pytest.approx(2.5), (
            "the duration must come from the injected clock, not from wall time"
        )

    asyncio.run(case())


def test_cancelled_capacity_wait_is_recorded() -> None:
    """Cancelling a wait must still record the time it was suspended."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        clock = ControllableMonotonicClock()
        sink = _RecordingSink()
        recorder = _RetentionTelemetry(
            exec_id=new_exec_id(),
            sinks=(sink,),
            clock=clock,
        )
        pending = [asyncio.create_task(_hold(gate))]
        await asyncio.sleep(0)

        waiter = asyncio.create_task(
            _await_hook_capacity(pending, 1, telemetry=recorder, phase=_STDOUT)
        )
        await asyncio.sleep(0)
        clock.advance(1.5)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert recorder.waits == 1, "an abandoned wait must still be counted"
        assert recorder.wait_seconds == pytest.approx(1.5), (
            "the recorded duration must cover the abandoned wait"
        )
        wait_samples = [
            sample for sample in sink.samples if sample.kind == _RETENTION_CAPACITY_WAIT
        ]
        assert [sample.outcome for sample in wait_samples] == ["cancelled"], (
            "the sample must record that the wait was cancelled, not resumed"
        )

        gate.set()
        await _wait_for_exec_hook_tasks(pending)

    asyncio.run(case())


def test_sinks_receive_every_bounded_sample() -> None:
    """Each recorded decision must reach an installed sink as one sample."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        sink = _RecordingSink()
        recorder = _RetentionTelemetry(exec_id=new_exec_id(), sinks=(sink,))
        pending = [asyncio.create_task(_hold(gate))]
        await asyncio.sleep(0)

        waiter = asyncio.create_task(
            _await_hook_capacity(pending, 1, telemetry=recorder, phase=_STDOUT)
        )
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(waiter, timeout=5.0)

        assert _RETENTION_DEPTH in sink.kinds(), (
            "every prune must publish the depth it measured"
        )
        assert _RETENTION_PRUNE in sink.kinds(), (
            "the ceiling re-check must publish a prune sample"
        )
        assert _RETENTION_SATURATION in sink.kinds(), (
            "the refusal that deferred the line must be published"
        )
        assert sink.samples[-1].kind == _RETENTION_CAPACITY_WAIT, (
            "the wait must be the last thing published when the line resumes"
        )
        assert sink.samples[-1].outcome == "resumed", (
            "the published wait must record how it ended"
        )
        assert all(sample.exec_id == recorder.exec_id for sample in sink.samples), (
            "every sample must correlate with the recorder's execution"
        )

    asyncio.run(case())
