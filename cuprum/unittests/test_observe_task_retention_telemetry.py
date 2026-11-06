"""Structured telemetry for retained observe-hook tasks.

An operator needs to tell "the run is slow" apart from "this run paused a
child because its observers were too slow", and to see that from logs alone.
These tests pin the recorder that answers that: the counters admission
increments, the depths it observes, and the fixed ``cuprum_*`` field set every
record carries. The rules the counters describe are tested in
``test_observe_task_retention.py`` and their composed-callback use in
``test_observe_task_retention_callbacks.py``.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from cuprum._observability import _wait_for_exec_hook_tasks
from cuprum._observe_task_retention import (
    _admit_hook_capacity,
    _await_hook_capacity,
    _prune_finished_hook_tasks,
)
from cuprum._retention_telemetry import _RetentionTelemetry
from cuprum.events import new_exec_id
from cuprum.unittests.test_line_event_emission_support import _make_observation
from cuprum.unittests.test_observe_task_retention import (
    _fail,
    _hold,
    _HookFailureError,
    _succeed,
)

_LOGGER = "cuprum._retention_telemetry"

# Every record this module writes carries these fields, so an operator can
# correlate one run's retention decisions without parsing the message text.
_EXPECTED_FIELDS = frozenset({
    "cuprum_exec_id",
    "cuprum_phase",
    "cuprum_ceiling",
    "cuprum_depth",
    "cuprum_max_depth",
    "cuprum_admissions",
    "cuprum_saturations",
    "cuprum_pruned",
    "cuprum_pruned_failures",
    "cuprum_waits",
    "cuprum_wait_seconds",
})


def _recorder() -> _RetentionTelemetry:
    """Build a fresh recorder for a synthetic execution."""
    return _RetentionTelemetry(exec_id=new_exec_id())


def _record_starting(
    caplog: pytest.LogCaptureFixture,
    prefix: str,
) -> logging.LogRecord:
    """Return the first captured record whose message starts with ``prefix``."""
    for record in caplog.records:
        if record.getMessage().startswith(prefix):
            return record
    msg = f"no record starting with {prefix!r}: {caplog.text!r}"
    raise AssertionError(msg)


def test_depth_counts_only_unfinished_tasks() -> None:
    """A settled task must not inflate the depth an operator reads."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        settled = asyncio.create_task(_succeed())
        held = asyncio.create_task(_hold(gate))
        await asyncio.sleep(0)
        recorder = _recorder()

        assert recorder.observe_depth([settled, held]) == 1, (
            "only the unfinished task may count towards the depth"
        )
        assert recorder.depth == 1, "the latest depth must be retained"
        assert recorder.max_depth == 1, "the high-water mark must be recorded"

        gate.set()
        await _wait_for_exec_hook_tasks([held])
        assert recorder.observe_depth([settled, held]) == 0, (
            "a completed task must leave the depth"
        )
        assert recorder.max_depth == 1, "the high-water mark must not fall back"

    asyncio.run(case())


def test_admission_records_saturation_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused admission must be counted and logged with the fixed fields."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        recorder = _recorder()
        pending = [asyncio.create_task(_hold(gate)) for _ in range(2)]
        await asyncio.sleep(0)

        admitted = _admit_hook_capacity(pending, 2, telemetry=recorder, phase="stdout")

        assert admitted is False, "two unfinished tasks must refuse admission"
        assert recorder.admissions == 1, "every decision must be counted"
        assert recorder.saturations == 1, "the refusal must be counted separately"
        assert recorder.depth == 2, "the decision must observe the real depth"
        gate.set()
        await _wait_for_exec_hook_tasks(pending)

    asyncio.run(case())

    record = _record_starting(caplog, "observe_task_admission")
    assert record.levelno == logging.DEBUG, "admission records must stay at debug"
    missing = _EXPECTED_FIELDS - vars(record).keys()
    assert not missing, f"the record must carry every fixed field, missing {missing}"
    assert vars(record)["cuprum_admitted"] is False, (
        "the per-record decision must be prefixed like every other field"
    )
    assert vars(record)["cuprum_phase"] == "stdout"
    assert vars(record)["cuprum_ceiling"] == 2


def test_prune_records_dropped_and_failed_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A prune must distinguish every dropped task from dropped failures."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        recorder = _recorder()
        success = asyncio.create_task(_succeed())
        first = asyncio.create_task(_fail("first"))
        second = asyncio.create_task(_fail("second"))
        await asyncio.sleep(0)
        tasks = [success, first, second]

        pruned = _prune_finished_hook_tasks(
            tasks, telemetry=recorder, phase="stderr", ceiling=3
        )

        assert pruned == 2, "the success and the later failure must both be dropped"
        assert recorder.pruned == 2, "every drop must be counted"
        assert recorder.pruned_failures == 1, (
            "only the dropped failure is a dropped failure"
        )
        with pytest.raises(_HookFailureError):
            await _wait_for_exec_hook_tasks(tasks)

    asyncio.run(case())

    record = _record_starting(caplog, "observe_task_pruned")
    assert vars(record)["cuprum_pruned"] == 2
    assert vars(record)["cuprum_pruned_failures"] == 1
    assert vars(record)["cuprum_prune_delta"] == 2, (
        "the per-prune count must not overwrite the running total"
    )


def test_capacity_wait_records_one_wait_and_its_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One suspended line must produce one wait, not one per re-check."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        recorder = _recorder()
        pending = [asyncio.create_task(_hold(gate))]
        await asyncio.sleep(0)

        waiter = asyncio.create_task(
            _await_hook_capacity(pending, 1, telemetry=recorder, phase="stdout")
        )
        await asyncio.sleep(0)
        assert recorder.waits == 0, "the wait is recorded only when it completes"

        gate.set()
        await asyncio.wait_for(waiter, timeout=5.0)

        assert recorder.waits == 1, "one suspended line must be one wait"
        assert recorder.wait_seconds >= 0.0, "a wait cannot have negative duration"
        assert recorder.admissions == 1, (
            "only the decision that refused the line may be counted; re-checks "
            "after each completion must not inflate the total"
        )

    asyncio.run(case())

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        message.startswith("observe_task_capacity_wait") for message in messages
    ), "the transition into backpressure must be logged"
    assert any(
        message.startswith("observe_task_capacity_resumed") for message in messages
    ), "the wait's exit and duration must be logged"
    wait_record = _record_starting(caplog, "observe_task_capacity_wait")
    assert wait_record.levelno == logging.INFO, (
        "entering backpressure is an operator-visible event, not debug noise"
    )
    resumed = _record_starting(caplog, "observe_task_capacity_resumed")
    assert resumed.levelno == logging.DEBUG, "the exit detail belongs at debug"
    assert vars(resumed)["cuprum_waits"] == 1
    assert vars(resumed)["cuprum_wait_seconds"] >= 0.0


def test_stage_observation_reuses_one_lazily_created_recorder() -> None:
    """Each stage must record against one timeline, not a fresh one per event."""
    observation = _make_observation()

    first = observation.retention_telemetry
    second = observation.retention_telemetry

    assert first is second, "the recorder must be created once and reused"
    assert first.exec_id == observation.exec_id, (
        "the recorder must correlate with the stage's execution events"
    )
