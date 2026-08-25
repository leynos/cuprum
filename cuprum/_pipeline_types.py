"""Internal data structures shared across pipeline execution helpers.

These small dataclasses model the hooks, per-stage observation state, and
captured results threaded through pipeline coordination. They live apart from
``cuprum._pipeline_internals`` so the coordination logic stays within the
project file-size ceiling and so spawn-side modules can import them at the
top level without import cycles; ``cuprum._pipeline_internals`` re-exports
them for backwards compatibility.
"""

from __future__ import annotations

import dataclasses as dc
import types
import typing as typ

from cuprum._observability import _emit_exec_event, _ExecEventEmissionError
from cuprum.events import ExecEvent, ExecPhase, TimeoutMode, new_exec_id


class _ExecutionInvariantError(RuntimeError):
    """Base class for internal pipeline and subprocess invariant failures."""


if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc
    from pathlib import Path

    from cuprum._pipeline_wait import _PipelineWaitResult
    from cuprum.context import AfterHook, BeforeHook
    from cuprum.events import ExecHook, ExecId
    from cuprum.sh import SafeCmd


@typ.runtime_checkable
class _PipelineWaitReporter(typ.Protocol):
    """Port for an adapter that renders pipeline-wait completion records."""

    def report_pipeline_wait(
        self,
        message: str,
        args: tuple[object, ...],
        extra: cabc.Mapping[str, object],
    ) -> None:
        """Render one pipeline-wait completion record."""


@dc.dataclass(frozen=True, slots=True)
class _ExecutionHooks:
    """Hooks resolved from the active context for a single command."""

    before_hooks: tuple[BeforeHook, ...]
    after_hooks: tuple[AfterHook, ...]
    observe_hooks: tuple[ExecHook, ...]


@dc.dataclass(frozen=True, slots=True)
class _EventDetails:
    """Optional per-event fields attached to an observe event."""

    pid: int | None
    line: str | None = None
    exit_code: int | None = None
    duration_s: float | None = None
    note: str | None = None
    byte_count: int | None = None
    operation: str | None = None
    error_type: str | None = None
    timeout_s: float | None = None
    timeout_mode: TimeoutMode | None = None
    # Only the pipeline fail-fast decision sets these; a stage's own lifecycle
    # events carry its position in ``tags`` instead.
    stage_index: int | None = None
    stage_count: int | None = None


@dc.dataclass(frozen=True, slots=True)
class _StageObservation:
    """Per-stage state used to emit observe events for a pipeline command."""

    cmd: SafeCmd
    hooks: _ExecutionHooks
    tags: cabc.Mapping[str, object]
    cwd: Path | None
    env_overlay: cabc.Mapping[str, str] | None
    pending_tasks: list[asyncio.Task[None]]
    wall_clock: cabc.Callable[[], float]
    # Minted once per stage observation so every lifecycle event this object
    # emits shares one correlation token, distinguishing this execution from
    # any other that happens to reuse the same PID.
    exec_id: ExecId = dc.field(default_factory=new_exec_id)

    def emit(
        self,
        phase: ExecPhase,
        details: _EventDetails,
    ) -> None:
        """Emit an observe event for ``phase`` when observe hooks are set."""
        if not self.hooks.observe_hooks:
            return
        event = ExecEvent(
            phase=phase,
            program=self.cmd.program,
            argv=self.cmd.argv_with_program,
            cwd=self.cwd,
            env=self.env_overlay,
            pid=details.pid,
            timestamp=self.wall_clock(),
            line=details.line,
            exit_code=details.exit_code,
            duration_s=details.duration_s,
            tags=self.tags,
            project=self.cmd.project.name,
            note=details.note,
            byte_count=details.byte_count,
            operation=details.operation,
            error_type=details.error_type,
            timeout_s=details.timeout_s,
            timeout_mode=details.timeout_mode,
            exec_id=self.exec_id,
            stage_index=details.stage_index,
            stage_count=details.stage_count,
        )
        self._emit_event(event)

    def emit_fail_fast(self, details: _EventDetails) -> None:
        """Emit the sanitized fail-fast decision event."""
        if not self.hooks.observe_hooks:
            return
        event = ExecEvent(
            phase="pipeline_fail_fast",
            program=self.cmd.program,
            argv=(),
            cwd=None,
            env=None,
            pid=details.pid,
            timestamp=self.wall_clock(),
            line=None,
            exit_code=details.exit_code,
            duration_s=details.duration_s,
            tags=types.MappingProxyType({}),
            project=self.cmd.project.name,
            note=None,
            byte_count=None,
            operation=None,
            error_type=None,
            timeout_s=None,
            timeout_mode=None,
            exec_id=self.exec_id,
            stage_index=details.stage_index,
            stage_count=details.stage_count,
        )
        self._emit_event(event)

    def report_pipeline_wait(
        self,
        message: str,
        args: tuple[object, ...],
        extra: cabc.Mapping[str, object],
    ) -> None:
        """Route a completion record through installed adapter ports."""
        for hook in self.hooks.observe_hooks:
            if isinstance(hook, _PipelineWaitReporter):
                hook.report_pipeline_wait(message, args, extra)

    def _emit_event(self, event: ExecEvent) -> None:
        """Dispatch one event and retain scheduled observe-hook tasks."""
        try:
            scheduled_tasks = _emit_exec_event(self.hooks.observe_hooks, event)
        except _ExecEventEmissionError as exc:
            self.pending_tasks.extend(exc.scheduled_tasks)
            raise exc.error from exc
        self.pending_tasks.extend(scheduled_tasks)


@dc.dataclass(frozen=True, slots=True)
class _PipelineStageResultInputs:
    """Aggregated wait outcome and captured output for stage results."""

    wait_result: _PipelineWaitResult
    stderr_by_stage: tuple[str | None, ...]
    final_stdout: str | None


@dc.dataclass(frozen=True, slots=True)
class _StageWaitContext:
    """Per-stage data the wait path reads, all indexed by stage.

    Every field is immutable, so the context stays a snapshot the wait path can
    only read. ``_PipelineWaitState`` copies ``started_at`` into its own list
    rather than aliasing it, which is what stops its live bookkeeping writing
    back through this supposedly frozen record.

    ``started_at`` is what stage durations are measured from. ``observations``
    provides the wait path with the hook set and stage execution token for the
    fail-fast report. It remains optional so transition tests and the symbolic
    model can construct a context without observability state.
    """

    started_at: tuple[float, ...]
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
    """Processes and output tasks produced when spawning a pipeline."""

    processes: list[asyncio.subprocess.Process]
    stderr_tasks: list[asyncio.Task[str | None] | None]
    stdout_task: asyncio.Task[str | None] | None
    stages: _StageWaitContext


@dc.dataclass(frozen=True, slots=True)
class _PipelineOutputs:
    """Captured outputs from pipeline execution."""

    stderr_by_stage: tuple[str | None, ...]
    final_stdout: str | None
    capture: bool
