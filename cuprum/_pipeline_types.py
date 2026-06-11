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
import time
import typing as typ

from cuprum._observability import _emit_exec_event, _ExecEventEmissionError
from cuprum.context import current_context
from cuprum.events import ExecEvent

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc
    from pathlib import Path

    from cuprum._pipeline_wait import _PipelineWaitResult
    from cuprum.context import AfterHook, BeforeHook
    from cuprum.events import ExecHook
    from cuprum.sh import SafeCmd


@dc.dataclass(frozen=True, slots=True)
class _ExecutionHooks:
    """Hooks resolved from the active context for a single command."""

    before_hooks: tuple[BeforeHook, ...]
    after_hooks: tuple[AfterHook, ...]
    observe_hooks: tuple[ExecHook, ...]


def _run_before_hooks(cmd: SafeCmd) -> _ExecutionHooks:
    """Collect hooks for a command after enforcing the current allowlist."""
    ctx = current_context()
    ctx.check_allowed(cmd.program)
    return _ExecutionHooks(
        before_hooks=ctx.before_hooks,
        after_hooks=ctx.after_hooks,
        observe_hooks=ctx.observe_hooks,
    )


@dc.dataclass(frozen=True, slots=True)
class _EventDetails:
    """Optional per-event fields attached to an observe event."""

    pid: int | None
    line: str | None = None
    exit_code: int | None = None
    duration_s: float | None = None
    note: str | None = None
    byte_count: int | None = None


@dc.dataclass(frozen=True, slots=True)
class _StageObservation:
    """Per-stage state used to emit observe events for a pipeline command."""

    cmd: SafeCmd
    hooks: _ExecutionHooks
    tags: cabc.Mapping[str, object]
    cwd: Path | None
    env_overlay: cabc.Mapping[str, str] | None
    pending_tasks: list[asyncio.Task[None]]

    def emit(
        self,
        phase: typ.Literal[
            "plan",
            "start",
            "stdout",
            "stderr",
            "exit",
            "stdin",
            "stdin_error",
        ],
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
            timestamp=time.time(),
            line=details.line,
            exit_code=details.exit_code,
            duration_s=details.duration_s,
            tags=self.tags,
            note=details.note,
            byte_count=details.byte_count,
        )
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
class _PipelineSpawnResult:
    """Processes and output tasks produced when spawning a pipeline."""

    processes: list[asyncio.subprocess.Process]
    stderr_tasks: list[asyncio.Task[str | None] | None]
    stdout_task: asyncio.Task[str | None] | None
    started_at: list[float]


@dc.dataclass(frozen=True, slots=True)
class _PipelineOutputs:
    """Captured outputs from pipeline execution."""

    stderr_by_stage: tuple[str | None, ...]
    final_stdout: str | None
    capture: bool
