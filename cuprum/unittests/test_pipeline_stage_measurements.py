"""Deterministic contracts for pipeline-stage timing and resource results.

A live pipeline cannot pin exact timestamps: stage starts come from
``time.time()`` and durations from ``time.perf_counter()``, so tests against a
real run can only assert ranges. These tests instead drive the result-building
seam directly with injected wait results, which turns every published value
into an exact literal and covers the failure and incomplete-stage cases without
spawning anything.

The injected values are deliberately distinct per stage and per clock —
wall-clock starts, monotonic starts, and completion instants all differ, and
the wall-clock and monotonic values share no magnitude — so a result built from
the wrong index, or read from the wrong clock, cannot pass by coincidence.
Every injected value is exactly representable in binary floating point, and
each duration is the difference of two such values, so the comparisons are
exact rather than approximate on purpose: a tolerance would let the very
defects these tests exist to catch slip through.
"""

from __future__ import annotations

import types
import typing as typ

from cuprum import ECHO, sh
from cuprum._pipeline_results import (
    _build_pipeline_stage_results,
    _emit_timeout_exit_events,
)
from cuprum._pipeline_types import (
    _ExecutionHooks,
    _PipelineSpawnResult,
    _PipelineStageResultInputs,
    _PipelineWaitResult,
    _StageObservation,
    _StageWaitContext,
)

if typ.TYPE_CHECKING:
    import asyncio

    import pytest

    from cuprum.events import ExecEvent, ExecHook
    from cuprum.sh import CommandResult, SafeCmd

# Injected clocks, one entry per stage of the widest pipeline built here.
# The wall-clock values are unrelated in magnitude to the monotonic ones, so
# reading the wrong clock is visible rather than plausible.
_WALL_CLOCK = (1_700_000_000.25, 1_700_000_001.5, 1_700_000_002.75)
_MONOTONIC_START = (10.0, 20.0, 30.0)
_MONOTONIC_END = (12.5, 24.0, 33.25)
# Each stage's duration is its own end minus its own start.
_EXPECTED_DURATIONS = (2.5, 4.0, 3.25)
# The instant the time-out path observes, measured against each stage's start.
# Stage 1 started after that instant, so it exercises the zero clamp.
_TIMEOUT_OBSERVED_AT = 12.5
_TIMEOUT_DURATIONS = (2.5, 0.0)
_FROZEN_EVENT_TIME = 1_700_000_100.0


def _exact(actual: float, expected: float, message: str) -> None:
    """Assert bit-exact equality between two injected timing values."""
    assert actual == expected, f"{message}: expected {expected!r}, found {actual!r}"


def _observations(
    stage_count: int,
    observe: tuple[ExecHook, ...] = (),
) -> tuple[_StageObservation, ...]:
    """Build one observation per stage, emitting terminal events to ``observe``."""
    builder = sh.make(ECHO)
    hooks = _ExecutionHooks(
        before_hooks=(),
        after_hooks=(),
        observe_hooks=observe,
    )
    return tuple(
        _StageObservation(
            cmd=builder("stage", str(idx)),
            hooks=hooks,
            tags={"pipeline_stage_index": idx},
            cwd=None,
            env_overlay=None,
            pending_tasks=[],
            wall_clock=_frozen_wall_clock,
        )
        for idx in range(stage_count)
    )


def _frozen_wall_clock() -> float:
    """Return the deterministic event timestamp these observations report."""
    return _FROZEN_EVENT_TIME


def _processes(
    observations: tuple[_StageObservation, ...],
) -> list[asyncio.subprocess.Process]:
    """Return PID-bearing process stand-ins, one per observation."""
    return typ.cast(
        "list[asyncio.subprocess.Process]",
        [types.SimpleNamespace(pid=1000 + idx) for idx in range(len(observations))],
    )


def _inputs(
    *,
    exit_codes: tuple[int, ...],
    ended_at: tuple[float | None, ...],
) -> _PipelineStageResultInputs:
    """Build the wait outcome the assembler consumes for a synthetic pipeline."""
    return _PipelineStageResultInputs(
        wait_result=_PipelineWaitResult(
            exit_codes=exit_codes,
            failure_index=None,
            started_at=_MONOTONIC_START,
            ended_at=ended_at,
            wall_clock_started_at=_WALL_CLOCK,
        ),
        stderr_by_stage=tuple(f"stderr-{idx}" for idx in range(len(exit_codes))),
        final_stdout="captured",
    )


def _build(
    *,
    exit_codes: tuple[int, ...],
    ended_at: tuple[float | None, ...],
    observe: tuple[ExecHook, ...] = (),
) -> list[CommandResult]:
    """Assemble the published stage results for a synthetic pipeline."""
    observations = _observations(len(exit_codes), observe)
    parts: tuple[SafeCmd, ...] = tuple(obs.cmd for obs in observations)
    return _build_pipeline_stage_results(
        parts,
        observations,
        processes=_processes(observations),
        inputs=_inputs(exit_codes=exit_codes, ended_at=ended_at),
    )


def test_every_stage_publishes_its_own_injected_timing() -> None:
    """Each stage reports its own wall-clock start and monotonic duration."""
    results = _build(exit_codes=(0, 0), ended_at=_MONOTONIC_END)

    assert len(results) == 2, "one result per pipeline stage"
    for idx, result in enumerate(results):
        _exact(result.started_at, _WALL_CLOCK[idx], f"stage {idx} start")
        _exact(result.duration, _EXPECTED_DURATIONS[idx], f"stage {idx} duration")


def test_failed_stage_still_publishes_its_own_timing() -> None:
    """A non-zero exit code changes nothing about how timing is attributed."""
    results = _build(exit_codes=(0, 1), ended_at=_MONOTONIC_END)

    assert [result.exit_code for result in results] == [0, 1], (
        "each stage must retain its own exit code"
    )
    for idx, result in enumerate(results):
        _exact(result.started_at, _WALL_CLOCK[idx], f"failing stage {idx} start")
        _exact(
            result.duration,
            _EXPECTED_DURATIONS[idx],
            f"failing stage {idx} duration",
        )


def test_incomplete_stage_reports_zero_duration_and_keeps_its_start() -> None:
    """A stage with no recorded completion reports ``0.0``, not a sibling's time.

    A stage terminated before its end was observed has still begun, so its own
    start timestamp is published; inheriting the neighbouring stage's duration
    would be a fabrication.
    """
    results = _build(exit_codes=(0, -15), ended_at=(_MONOTONIC_END[0], None))

    _exact(results[0].duration, _EXPECTED_DURATIONS[0], "completed stage duration")
    _exact(results[1].started_at, _WALL_CLOCK[1], "incomplete stage start")
    _exact(results[1].duration, 0.0, "incomplete stage duration")


def test_stage_events_carry_the_same_injected_timing_as_the_result() -> None:
    """The terminal event a stage emits agrees with the result built beside it."""
    events: list[ExecEvent] = []
    results = _build(
        exit_codes=(0, 1),
        ended_at=_MONOTONIC_END,
        observe=(events.append,),
    )

    assert [event.exit_code for event in events] == [0, 1], (
        "each stage's exit event must carry that stage's exit code"
    )
    assert len(events) == len(results), "each stage emits exactly one terminal event"
    for idx, (event, result) in enumerate(zip(events, results, strict=True)):
        assert event.duration_s is not None, "an exit event must publish a duration"
        _exact(
            event.duration_s,
            _EXPECTED_DURATIONS[idx],
            f"stage {idx} event duration",
        )
        _exact(
            event.duration_s,
            result.duration,
            f"stage {idx} event/result duration",
        )


def test_stages_never_publish_child_resource_measurements() -> None:
    """Stages leave every resource field unset, however they completed.

    Concurrently reaped children cannot be attributed to one stage, so the
    fields stay ``None``. Driving the assembler directly shows the omission is
    structural rather than incidental to a particular run.
    """
    results = _build(
        exit_codes=(0, 1, -15),
        ended_at=(*_MONOTONIC_END[:2], None),
    )

    for idx, result in enumerate(results):
        _exact(result.started_at, _WALL_CLOCK[idx], f"stage {idx} start")
        assert result.max_rss_bytes is None, f"stage {idx} must not claim child RSS"
        assert result.user_cpu_seconds is None, f"stage {idx} must not claim user CPU"
        assert result.system_cpu_seconds is None, f"stage {idx} must not claim sys CPU"


def test_timeout_events_publish_each_stages_own_injected_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A time-out reports every terminated stage measured from its own start.

    This is the second of the two paths that publish a stage duration, and the
    only one a terminated — hence incomplete — stage takes. The clock is pinned
    to an instant after stage 0 started but before stage 1 started, so the two
    stages must produce different durations and stage 1 must clamp at zero.

    ``_pipeline_results`` reaches the clock as ``time.perf_counter`` through the
    imported module rather than binding it locally, so the stdlib attribute is
    the only seam available; ``monkeypatch`` restores it on return.
    """
    monkeypatch.setattr(
        "cuprum._pipeline_results.time.perf_counter",
        lambda: _TIMEOUT_OBSERVED_AT,
    )
    events: list[ExecEvent] = []
    observations = _observations(2, (events.append,))
    spawn = _PipelineSpawnResult(
        processes=typ.cast(
            "list[asyncio.subprocess.Process]",
            [
                types.SimpleNamespace(pid=1, returncode=0),
                # A stage with no published code falls back to -1.
                types.SimpleNamespace(pid=2, returncode=None),
            ],
        ),
        stderr_tasks=[],
        stdout_task=None,
        stages=_StageWaitContext(started_at=_MONOTONIC_START),
    )

    _emit_timeout_exit_events(observations, spawn)

    assert [event.exit_code for event in events] == [0, -1], (
        "a stage without a recorded code must report -1"
    )
    assert len(events) == len(observations), "every terminated stage reports an exit"
    for idx, event in enumerate(events):
        assert event.duration_s is not None, "a time-out exit event owes a duration"
        _exact(
            event.duration_s,
            _TIMEOUT_DURATIONS[idx],
            f"stage {idx} timeout duration",
        )
