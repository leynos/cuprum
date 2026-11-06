"""Bound per-stream emitter for line ``ExecEvent`` dispatch.

One observed stream emits an event per decoded line, and almost every field of
that event is fixed for the stream's whole life: the program, its resolved argv,
the execution's environment policy, the correlation token, and the dispatcher
that fans the event out to observe hooks. Rebuilding those per line is pure
overhead, so :class:`_LineEventEmitter` binds them once at preparation time and
completes the event with only a line and a timestamp.

The emitter also owns the two admission calls line observation makes against
the run-wide retention list. They live here rather than in the composing module
because the bound is a property of emitting: a stream that never emits a line
event never needs a slot. The ceiling, pruning rule, and wait live in
:mod:`cuprum._observe_task_retention`.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum._observe_task_retention import (
    _OBSERVE_TASK_CEILING,
    _admit_hook_capacity,
    _await_hook_capacity,
)
from cuprum.events import ExecEvent

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc
    from pathlib import Path

    from cuprum._retention_telemetry import _RetentionTelemetry
    from cuprum.context.env_overlay import EnvMode, EnvOverlay
    from cuprum.events import CaptureMode, ExecId
    from cuprum.lines import LineStreamName
    from cuprum.program import Program


@dc.dataclass(frozen=True, slots=True)
class _LineEventEmitter:
    """The invariant half of a line ``ExecEvent``, bound once per stream.

    Every field here is fixed for the life of one observed stream, so a line
    event needs only a line and a timestamp to complete it. ``emit`` rebuilds
    all of them — including ``argv_with_program``, which walks the whole argv
    tuple — for every line, which is the cost this type exists to remove.

    Attributes
    ----------
    program:
        The allowlisted program, read from the command once.
    argv:
        The full argv with the program name first, resolved once.
    cwd:
        Working directory for the execution, when set.
    env:
        Environment overlay for the execution, when set.
    env_mode:
        The effective environment policy for this execution. It is invariant
        per stage observation like the other fields here, and it is carried on
        *every* phase rather than only the terminal one, so each line event
        must report it or the mode would appear to change mid-stream.
    resolved_path:
        The executable this execution actually ran, when a binding named one.
        Invariant per stage observation for the same reason as ``env_mode``:
        resolution happens once, before the child is spawned, so a line event
        that omitted it would disagree with the ``plan`` and ``exit`` events
        around it about which executable ran.
    capture_mode:
        Whether this execution's captured streams are reported as text or as
        the child's own bytes. Invariant per stage observation for the same
        reason as ``env_mode``: it is decided before the child is spawned, so a
        line event that omitted it would report a different mode from the
        events around it.
    pid:
        Subprocess identifier, ``None`` before spawn.
    stream:
        Which output stream the lines arrive on; this doubles as the event
        phase, so no ``Literal["stdout", "stderr"]`` alias is needed.
    tags:
        The observation's tag mapping, shared not copied.
    project:
        Trusted project name, read from the command once.
    exec_id:
        Correlation token minted once per stage observation.
    wall_clock:
        The clock read once per emitted line.
    emit_event:
        The observation's own dispatcher, bound rather than called, so the
        emission still runs through ``_emit_event``.
    pending_tasks:
        The run-wide list the observation retains its async hook tasks on.
        It is shared by every stage of a pipeline, so capacity measured here
        is capacity for the whole run rather than for this stream alone.
    telemetry:
        The stage's retention recorder, used for the depth and admission
        records this emitter's calls produce.

    """

    program: Program
    argv: tuple[str, ...]
    cwd: Path | None
    env: EnvOverlay | None
    env_mode: EnvMode | None
    resolved_path: str | None
    capture_mode: CaptureMode | None
    pid: int | None
    stream: LineStreamName
    tags: cabc.Mapping[str, object]
    project: str
    exec_id: ExecId
    wall_clock: cabc.Callable[[], float]
    emit_event: cabc.Callable[[ExecEvent], None]
    pending_tasks: list[asyncio.Task[None]]
    telemetry: _RetentionTelemetry

    def admit_observation(self) -> bool:
        """Prune at the ceiling and report whether this line may dispatch now.

        This is a command: reaching the ceiling prunes settled observer tasks
        in place before the slot count is judged. The name says so, because a
        caller must not read it as a pure capacity query.

        Returns
        -------
        bool
            ``True`` when this line's observation may be dispatched now.
        """
        return _admit_hook_capacity(
            self.pending_tasks,
            _OBSERVE_TASK_CEILING,
            telemetry=self.telemetry,
            phase=self.stream,
        )

    async def await_capacity(self) -> None:
        """Wait until another observer task may be scheduled."""
        await _await_hook_capacity(
            self.pending_tasks,
            _OBSERVE_TASK_CEILING,
            telemetry=self.telemetry,
            phase=self.stream,
        )

    def emit_line(self, line: str) -> None:
        """Emit one line event from the values bound at preparation time."""
        self.emit_event(
            ExecEvent(
                phase=self.stream,
                program=self.program,
                argv=self.argv,
                cwd=self.cwd,
                env=self.env,
                env_mode=self.env_mode,
                resolved_path=self.resolved_path,
                capture_mode=self.capture_mode,
                pid=self.pid,
                timestamp=self.wall_clock(),
                line=line,
                exit_code=None,
                duration_s=None,
                tags=self.tags,
                project=self.project,
                exec_id=self.exec_id,
            ),
        )


__all__ = ["_LineEventEmitter"]
