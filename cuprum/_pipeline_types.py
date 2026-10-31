"""Internal data structures shared across pipeline execution helpers.

These dataclasses model the hooks, per-stage observation state, and event
details threaded through pipeline coordination. They live apart from
``cuprum._pipeline_internals`` so the coordination logic stays within the
project file-size ceiling and so spawn-side modules can import them at the
top level without import cycles; ``cuprum._pipeline_internals`` re-exports
them for backwards compatibility.

The aggregate records those phases exchange — a spawn's processes, a wait's
exit codes, a pipeline's captured output — live in
``cuprum._pipeline_records``, which is split out for the same ceiling and
depends on the observation types defined here.
"""

from __future__ import annotations

import dataclasses as dc
import time
import types
import typing as typ

from cuprum._observability import (
    _bound_executable,
    _emit_exec_event,
    _ExecEventEmissionError,
)
from cuprum.events import (
    CaptureMode,
    ExecEvent,
    ExecPhase,
    GroupTeardownOutcome,
    ResourceUsageMode,
    StdioFailureCategory,
    TerminalOutcome,
    TimeoutMode,
    new_exec_id,
)


class _ExecutionInvariantError(RuntimeError):
    """Base class for internal pipeline and subprocess invariant failures."""


if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.context import AfterHook, BeforeHook

    # Annotation-only, as in ``cuprum.events``: ``env_mode`` is read as a value
    # off the event, never resolved as a hint, so the names stay out of the
    # runtime scope.
    from cuprum.context.env_overlay import EnvMode, EnvOverlay
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
    eof_grace_s: float | None = None
    pending_readers: int | None = None
    max_rss_bytes: int | None = None
    user_cpu_seconds: float | None = None
    system_cpu_seconds: float | None = None
    resource_usage_mode: ResourceUsageMode | None = None
    # Appended so every existing positional construction keeps its slots; see
    # the note on ``ExecEvent``. ``group_outcome`` is the closed enum rather
    # than a string because it labels a bounded metric.
    owns_group: bool | None = None
    group_outcome: GroupTeardownOutcome | None = None
    settle_s: float | None = None
    # Appended at the end for the reason ``ExecEvent`` documents: a positional
    # construction would otherwise rebind the field ahead of it. Only the
    # ``stdio_error`` diagnostics set this.
    error_category: StdioFailureCategory | None = None


@dc.dataclass(slots=True)
class _StageObservation:
    """Per-stage state used to emit observe events for a pipeline command."""

    cmd: SafeCmd
    hooks: _ExecutionHooks
    tags: cabc.Mapping[str, object]
    cwd: Path | None
    env_overlay: EnvOverlay | None
    pending_tasks: list[asyncio.Task[None]]
    wall_clock: cabc.Callable[[], float]
    # The monotonic counterpart of ``wall_clock``, and the seam a run's own
    # deadline is measured through: the start reference and the remaining-time
    # query both read it, so a timeout can be driven by an injected clock
    # rather than by the machine's. Both production builders resolve it at
    # their construction site rather than relying on this default, which binds
    # when the class is created and would ignore a patched ``time``. A
    # construction that supplies no clock still gets a usable one.
    # Per-line ``LineEvent.at`` stamps read this same callable, because the
    # start reference they subtract is sampled from it: reading the stamp from
    # a second clock would make it a difference between two clocks.
    monotonic_clock: cabc.Callable[[], float] = time.perf_counter
    # Minted once per stage observation so every lifecycle event this object
    # emits shares one correlation token, distinguishing this execution from
    # any other that happens to reuse the same PID.
    exec_id: ExecId = dc.field(default_factory=new_exec_id)
    # Appended after ``exec_id`` rather than placed beside ``env_overlay``, for
    # the reason ``ExecEvent`` documents: a positional construction would
    # otherwise rebind ``exec_id`` and hand the correlation token to the mode.
    # The value is the composed policy the execution actually runs under,
    # resolved once from the same call that produced ``env_overlay``. It is a
    # trusted field rather than a tag because a caller may shadow any tag key,
    # and the point of publishing the mode is that a consumer can rely on it.
    env_mode: EnvMode | None = None
    _plan_emitted: bool = dc.field(default=False, init=False)
    _terminal_emitted: bool = dc.field(default=False, init=False)
    _started_pid: int | None = dc.field(default=None, init=False)
    # Resolved once, here, so a resolver runs exactly once per execution no
    # matter how many lifecycle events the stage emits. ``None`` means the
    # program was unbound and runs under its catalogued name, which
    # ``argv0`` turns into ``str(cmd.program)``.
    resolved_path: str | None = None
    # Whether this stage's captured streams are reported as text or as the
    # child's own bytes. Like ``env_mode`` it is a trusted typed field rather
    # than a tag, because a caller may shadow any tag key and a consumer of the
    # capture mode must be able to rely on it. ``None`` only for a hand-built
    # observation; both builders resolve it.
    capture_mode: CaptureMode | None = None

    @property
    def argv0(self) -> str:
        """The executable this stage's child is spawned with.

        The spawn sites read this rather than ``resolved_path`` so the
        fallback rule — an unbound program runs under its catalogued name —
        exists once. It lives in ``_bound_executable`` because the detached
        launch reaches the same rule without an observation to read it from.
        """
        return _bound_executable(self.cmd.program, self.resolved_path)

    def emit(
        self,
        phase: ExecPhase,
        details: _EventDetails,
        *,
        terminal_outcome: TerminalOutcome | None = None,
    ) -> None:
        """Emit an observe event for ``phase`` when observe hooks are set."""
        if not self.hooks.observe_hooks:
            return
        match phase:
            case "plan":
                # Mark before dispatch: if a synchronous observer raises, the
                # runner still owes a best-effort terminal event for the plan
                # it began delivering.
                self._plan_emitted = True
            case "start":
                self._started_pid = details.pid
            case _:
                pass
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
            terminal_outcome=terminal_outcome,
            stage_index=details.stage_index,
            stage_count=details.stage_count,
            eof_grace_s=details.eof_grace_s,
            pending_readers=details.pending_readers,
            max_rss_bytes=details.max_rss_bytes,
            user_cpu_seconds=details.user_cpu_seconds,
            system_cpu_seconds=details.system_cpu_seconds,
            resource_usage_mode=details.resource_usage_mode,
            owns_group=details.owns_group,
            group_outcome=details.group_outcome,
            settle_s=details.settle_s,
            env_mode=self.env_mode,
            resolved_path=self.resolved_path,
            capture_mode=self.capture_mode,
            error_category=details.error_category,
        )
        self._emit_event(event)

    @property
    def started_pid(self) -> int | None:
        """Child PID announced by this observation, if any."""
        return self._started_pid

    def emit_terminal(
        self,
        outcome: TerminalOutcome,
        details: _EventDetails,
    ) -> None:
        """Emit the first and only definitive outcome for this observation."""
        if self._terminal_emitted or not self._plan_emitted:
            return
        self._terminal_emitted = True
        self.emit("settled", details, terminal_outcome=outcome)

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
            eof_grace_s=None,
            pending_readers=None,
            # The resource fields belong to the failing stage's own ``exit``
            # event, which follows this decision; a decision about a teardown
            # measures nothing, so they stay unset rather than being copied.
            max_rss_bytes=None,
            user_cpu_seconds=None,
            system_cpu_seconds=None,
            resource_usage_mode=None,
            # Unlike the environment itself, which this sanitized decision
            # event omits, the mode is a bounded policy name with no caller
            # data in it. A teardown decision is exactly where a consumer
            # needs it: whether the failing stage ran under a replacement
            # boundary is the first thing that explains a missing ``PATH``.
            env_mode=self.env_mode,
            # Carried for the same reason as ``env_mode``: it is a bounded
            # policy name with no caller data, and whether the failing stage
            # was collecting bytes is part of explaining its teardown.
            capture_mode=self.capture_mode,
            # Deliberately unset: this sanitized decision event describes no
            # stage's execution, and the failing stage reports its own path on
            # the ``exit`` event that follows. Carrying one here would invite a
            # consumer to read the pipeline's decision as having run that path.
            resolved_path=None,
            # A decision about a teardown has no failing stdio boundary of its
            # own; the stage's ``exit`` event that follows carries the fault.
            error_category=None,
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
