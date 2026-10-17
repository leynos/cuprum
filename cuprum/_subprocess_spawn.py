"""The spawn seam every direct child goes through.

Split from ``cuprum._subprocess_execution`` so the orchestration module stays
about timing, waiting, and result assembly while this one owns the one step
that creates a child: the execution bundle that decides how it is wired, the
spawn itself, and the announcement of its PID.

``_spawn_subprocess`` is reached by three entry points, and tests that need to
refuse a spawn patch it, so it lives apart from the result-assembly path rather
than beside it. ``_start_owned_process`` below serves the direct run, which is
the only path that announces its child here; the line-stream coordinator and
the owned-handle start each call ``_spawn_subprocess`` directly and emit their
own ``start`` event, because both need the process in hand before announcing it
— the coordinator to attach its consumers, the handle to attach the tasks that
will drive and settle the run. Nothing here waits for a child, reconciles its
tasks, or reads a clock: ``_start_owned_process`` takes the start reference it
stamps from its caller, because *when* that reading is taken is what its
meaning depends on and each caller takes it at a different point.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum import _wait4_spawn
from cuprum._context_policy import _merge_env
from cuprum._pipeline_types import _EventDetails
from cuprum._subprocess_context import _cwd_arg, _ownership_spawn_kwargs

# Imported at runtime, not under ``TYPE_CHECKING``: the policies below are this
# dataclass's own default values, so the names must resolve when the class body
# executes.
from cuprum.echo_events import BrokenPipePolicy
from cuprum.sh.execution import ProcessGroupPolicy

if typ.TYPE_CHECKING:
    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._pipeline_types import _StageObservation
    from cuprum.capture_policy import CapturePolicy
    from cuprum.lines import _LineHookFn
    from cuprum.sh import ExecutionContext, SafeCmd
    from cuprum.sinks.base import OutputSession


@dc.dataclass(frozen=True, slots=True)
class _SubprocessExecution:
    """Execution context bundle for subprocess spawning."""

    cmd: SafeCmd

    ctx: ExecutionContext

    # The resolved per-stream capture policies, mirroring how ``echo_stdout``
    # and ``echo_stderr`` already differ per stream. ``None`` for a stream the
    # run does not capture, so a single field pair expresses every combination
    # of the two streams rather than one flag both must share.
    capture_stdout: CapturePolicy | None

    capture_stderr: CapturePolicy | None

    echo_stdout: bool

    echo_stderr: bool

    max_echo_line_bytes: int | None

    sink_session: OutputSession | None

    timeout: float | None

    observation: _StageObservation

    stdin_data: bytes | None
    on_line: _LineHookFn | None = None
    # Mirrors ``ExecutionContext.process_group``. Carried on the bundle so the
    # spawn and the teardown of the same run cannot disagree about whether the
    # child's group belongs to this run.
    process_group: ProcessGroupPolicy = ProcessGroupPolicy.INHERIT
    # Whether this run's captured streams are reported as bytes rather than
    # decoded text. Defaulted for the tests that build this bundle directly and
    # for every text-mode caller; only the binary entry points set it.
    capture_bytes: bool = False
    # Defaulted for the tests that build this bundle directly, and resolved by
    # ``RunOutputOptions.__post_init__`` on the production path, so the value
    # reaching the stream config is always a member, never a raw spelling.
    broken_pipe_policy: BrokenPipePolicy = BrokenPipePolicy.STRICT
    # Monotonic reference the per-line ``at`` stamps are measured from; taken
    # once at spawn so every line of a run shares one time base.
    started_at: float = 0.0
    idle: _IdleMonitor | None = None

    @property
    def owns_process_group(self) -> bool:
        """Whether this run owns the child's process group.

        Derived from ``process_group`` rather than stored again, so the spawn
        decision and every teardown decision are read from one field and
        cannot fall out of step. False for a child that was never spawned
        under ``OWN_GROUP``, which is also the answer for a platform where
        that policy was rejected before any child existed.
        """
        return self.process_group is ProcessGroupPolicy.OWN_GROUP

    @property
    def consumes_stdout(self) -> bool:
        """Whether the parent must consume stdout, rather than discard it.

        A registered ``on_line`` observes both streams, so it keeps the pipe
        open and the consumer running even when capture and echo are both off.
        Without that, ``run(output=RunOutputOptions(on_line=...))`` would attach
        stdout to ``DEVNULL`` and silently deliver nothing.
        """
        return (
            self.capture_stdout is not None
            or self.echo_stdout
            or self.idle is not None
            or self.on_line is not None
        )

    @property
    def consumes_stderr(self) -> bool:
        """Whether the parent must consume stderr, rather than discard it."""
        return (
            self.capture_stderr is not None
            or self.echo_stderr
            or self.idle is not None
            or self.on_line is not None
        )


async def _spawn_subprocess(
    execution: _SubprocessExecution,
) -> asyncio.subprocess.Process:
    """Spawn an async subprocess with configured I/O and environment."""
    # ``consumes_stdout``/``consumes_stderr`` fold in the idle monitor as well as
    # capture and echo, so a run the watchdog narrates keeps its pipes.
    return await _wait4_spawn.spawn_direct_process(
        _wait4_spawn.DirectProcessConfig(
            argv=(execution.observation.argv0, *execution.cmd.argv),
            stdout=(
                asyncio.subprocess.PIPE
                if execution.consumes_stdout
                else asyncio.subprocess.DEVNULL
            ),
            stderr=(
                asyncio.subprocess.PIPE
                if execution.consumes_stderr
                else asyncio.subprocess.DEVNULL
            ),
            stdin=asyncio.subprocess.PIPE if execution.stdin_data is not None else None,
            env=_merge_env(execution.ctx.env, execution.ctx.env_mode),
            cwd=_cwd_arg(execution.ctx.cwd),
            # Raises before spawning when the platform cannot honour the
            # policy, so the failure names the option rather than arriving as
            # a run that quietly contained nothing.
            **_ownership_spawn_kwargs(execution.process_group),
        )
    )


@dc.dataclass(frozen=True, slots=True)
class _StartedRun:
    """A spawned child and the execution bundle stamped for it.

    The two travel together because they are only meaningful together: the
    bundle's ``started_at`` is the time base the child's line stamps and
    duration are measured from, and a caller holding the process without that
    stamping would be timing the child against whatever ``0.0`` default the
    bundle was built with.
    """

    process: asyncio.subprocess.Process
    execution: _SubprocessExecution


async def _start_owned_process(
    execution: _SubprocessExecution,
    started_at: float,
) -> _StartedRun:
    """Spawn a child, stamp the bundle its readers time themselves against.

    The ``started_at`` is taken by the caller rather than sampled here, because
    where it is sampled is what its meaning depends on: every caller takes it
    *before* the spawn await, so a run's recorded duration includes the time its
    spawn blocked. Passing it in keeps that ordering with the caller that owns
    it, and keeps this helper free of a clock read that could be reordered.

    Everything after the spawn is emission-free until the ``start`` event, so
    the child is never running without a stamped bundle and an announced PID to
    match it.

    Returns
    -------
    _StartedRun
        The spawned process and the rebuilt execution bundle whose
        ``started_at`` its stream consumers stamp each ``LineEvent`` with.
    """
    # Rebuilt, not mutated: the bundle is a frozen dataclass, and the stream
    # consumers read ``started_at`` off it when stamping each ``LineEvent``.
    # Left at its ``0.0`` default, every ``at`` would be the machine's monotonic
    # uptime rather than seconds since this command started. Stamping the
    # bundle reads no clock, so it does not disturb the caller's sampling order.
    stamped = dc.replace(execution, started_at=started_at)
    process = await _spawn_subprocess(stamped)
    stamped.observation.emit("start", _EventDetails(pid=process.pid))
    return _StartedRun(process=process, execution=stamped)


__all__ = [
    "_StartedRun",
    "_SubprocessExecution",
    "_spawn_subprocess",
    "_start_owned_process",
]
