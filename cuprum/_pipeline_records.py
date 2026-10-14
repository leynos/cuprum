"""Records carried between a pipeline's spawn, wait, and result assembly.

Split from ``cuprum._pipeline_types`` so that module holds the vocabulary a
per-stage observation is built from — the hooks, the event details, the
observation record itself — while this one holds the aggregate records that
carry a spawn's processes, a wait's exit codes, and a pipeline's captured
output across the phases that exchange them. The seam is a direction of
travel: the observation types are meaningful for a single stage with no
pipeline around it, while every record here aggregates over stages.

``_StageObservation`` is the one name crossing that seam, and it crosses for
annotation only, so the import stays under ``TYPE_CHECKING``. Keeping the
records out of ``cuprum._pipeline_types`` is also what lets
``cuprum._pipeline_wait`` import them directly: that module already imports
the observation types, so a record defined there could not be taken back from
the wait without closing a cycle.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._pipeline_types import _StageObservation
    from cuprum._streams import _RelayDiagnostics
    from cuprum._subprocess_wait_types import _StreamConsumerTask, _StreamPayload
    from cuprum.echo_events import RelayFallback


@dc.dataclass(frozen=True, slots=True)
class _PipelineWaitResult:
    """Exit codes and timing captured once a pipeline finishes waiting."""

    exit_codes: tuple[int, ...]
    failure_index: int | None
    started_at: tuple[float, ...]
    ended_at: tuple[float | None, ...]
    wall_clock_started_at: tuple[float, ...]


@dc.dataclass(frozen=True, slots=True)
class _PipelineStageResultInputs:
    """Aggregated wait outcome and captured output for stage results.

    ``relay_fallbacks_by_stage`` holds one tuple per pipeline stage, in stage
    order. A stage's tuple lists its stdout diagnostics first (final stage
    only, matching the single-command result order) and then its stderr
    diagnostics, so every stage result owns the records of its own streams.
    """

    wait_result: _PipelineWaitResult
    stderr_by_stage: tuple[_StreamPayload | None, ...]
    final_stdout: _StreamPayload | None
    relay_fallbacks_by_stage: tuple[tuple[RelayFallback, ...], ...]


@dc.dataclass(frozen=True, slots=True)
class _StageWaitContext:
    """Per-stage data the wait path reads, all indexed by stage.

    Every field is immutable, so the context stays a snapshot the wait path can
    only read. ``_PipelineWaitState`` copies ``started_at`` into its own list
    rather than aliasing it, which is what stops its live bookkeeping writing
    back through this supposedly frozen record.

    ``started_at`` is the monotonic time stage durations are measured from;
    ``wall_clock_started_at`` is the corresponding public result timestamp.
    ``observations`` provides the wait path with the hook set and stage
    execution token for the fail-fast report. It remains optional so transition
    tests and the symbolic model can construct a context without observability
    state.
    """

    started_at: tuple[float, ...]
    wall_clock_started_at: tuple[float, ...] = ()
    observations: tuple[_StageObservation, ...] = ()


@dc.dataclass(frozen=True, slots=True)
class _PipelineObservers:
    """Per-stage observations and the observe-hook tasks they schedule.

    The tasks live alongside the observations because every stage shares one
    list: :meth:`_StageObservation.emit` appends to it, and the runner drains
    that single collection on the way out however the pipeline ends.
    """

    observations: tuple[_StageObservation, ...]
    pending_tasks: list[asyncio.Task[None]]


@dc.dataclass(frozen=True, slots=True)
class _PipelineSpawnResult:
    """Processes and output tasks produced when spawning a pipeline.

    ``owns_group`` carries one flag per spawned stage, in stage order, so the
    teardown paths can signal each stage the way it was spawned. It defaults to
    empty for the callers that build this result directly: an empty sequence
    leaves every stage on the direct-child route, which is what those callers
    spawned.
    """

    processes: list[asyncio.subprocess.Process]
    stderr_tasks: list[_StreamConsumerTask | None]
    stdout_task: _StreamConsumerTask | None
    relay_diagnostics_by_stage: tuple[
        tuple[_RelayDiagnostics | None, _RelayDiagnostics | None],
        ...,
    ]
    stages: _StageWaitContext
    owns_group: tuple[bool, ...] = ()
    idle: _IdleMonitor | None = None


@dc.dataclass(frozen=True, slots=True)
class _PipelineOutputs:
    """Captured outputs from pipeline execution.

    ``relay_fallbacks_by_stage`` mirrors
    :class:`_PipelineStageResultInputs` so the timeout path can raise with
    whatever was already recorded before the wait failed.
    """

    stderr_by_stage: tuple[_StreamPayload | None, ...]
    final_stdout: _StreamPayload | None
    capture: bool
    # Whether the captured payloads are bytes rather than decoded text. Read
    # only where the ``TimeoutExpired`` is built, because that is the one
    # payload this path has to *combine* rather than pass through: joining
    # stage stderr needs the empty value of the mode it is joining in.
    capture_bytes: bool = False
    relay_fallbacks_by_stage: tuple[tuple[RelayFallback, ...], ...] = ()
