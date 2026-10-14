"""Spawning one child with the run's resolved stdio, and owning what it borrows.

Everything the parent does *around* a spawn without consuming a stream lives
here: the spawn call itself, when the stdio it hands over is prepared, and what
the spawned child becomes. The stdio mapping and the ownership rule — which
descriptor cuprum opened and must close, which it merely borrowed, and how a
binding becomes a value the spawn layer receives — live in
``cuprum._stdio_ownership``, which this module imports. The seam is the one the
code draws: that module owns *how a binding becomes a descriptor*, while this
one owns *when* that happens relative to the fork.

The split was forced by the 400-line ceiling after the rebase onto ``main``.
Because ``_spawn_subprocess`` is reached by three entry points and tests that
need to refuse a spawn patch it by this module's name, the moved helpers are
re-exported here rather than re-homed at every importer: ``pipes`` names the
streams cuprum really holds a parent-side pipe for, which is the one piece of
the resolution the value cannot express for itself, and ``Popen`` leaves its
own ``stdin``/``stdout``/``stderr`` as ``None`` for everything except ``PIPE``.
``create_subprocess_exec`` behaves the same way in this respect, yielding a
reader only for ``PIPE``, so this is not a divergence between the backends.

Nothing here waits for a child, reconciles its tasks, or reads a clock:
:func:`_start_owned_process` takes the start reference it stamps from its
caller, because *when* that reading is taken is what its meaning depends on and
each caller takes it at a different point.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum import _wait4_spawn
from cuprum._context_policy import _merge_env
from cuprum._pipeline_types import _EventDetails
from cuprum._stdio_ownership import (
    # Re-exported under this module's name as well as imported for use here.
    # Tests that refuse a spawn patch names on this module, and the ownership
    # helpers are what a refused spawn must not reach; a re-import elsewhere
    # would give them a second binding no call path reads.
    _close_owned_stdio,
    _flush_borrowed_stdio,
    _open_owned_stdio,
    _output_stdio,
    _stdin_stdio,
)
from cuprum._subprocess_context import _cwd_arg, _ownership_spawn_kwargs

# Imported at runtime, not under ``TYPE_CHECKING``: the two policies below are
# the bundle's own default values, so the names must resolve when the class body
# executes.
from cuprum.echo_events import BrokenPipePolicy
from cuprum.sh.execution import ProcessGroupPolicy

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._bounded_delivery import _DeliveryWorker
    from cuprum._constants import PipeStream
    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._pipeline_types import _StageObservation
    from cuprum._stream_drain_state import _RelayDiagnostics
    from cuprum._stdio_plan import _ResolvedStdio
    from cuprum.lines import _LineHookFn
    from cuprum.sh import ExecutionContext, SafeCmd
    from cuprum.sinks.base import OutputSession


@dc.dataclass(frozen=True, slots=True)
class _SubprocessExecution:
    """Execution context bundle for subprocess spawning.

    Every decision the spawn and the teardown of one run must agree on is
    carried here rather than read from the context twice. Two of those
    agreements are structural: *stdio* holds the run's resolved bindings, so the
    spawn maps them and the stream wiring asks the same object which streams
    cuprum holds a parent-side pipe for; and *process_group* is mirrored from
    :class:`~cuprum.sh.ExecutionContext` so neither stage can read a policy the
    other has already been built past.

    It lives here, beside the spawn that consumes it, rather than in
    ``cuprum._subprocess_execution``: the stream wiring is imported *by* that
    module, so a bundle defined there could not be typed in this module's
    writers without a cycle.
    """

    cmd: SafeCmd
    ctx: ExecutionContext
    capture: bool
    echo_stdout: bool
    echo_stderr: bool
    max_echo_line_bytes: int | None
    sink_session: OutputSession | None
    timeout: float | None
    observation: _StageObservation
    # The run's resolved standard streams: which of them cuprum owns a pipe for,
    # which the caller bound elsewhere, and where stdin comes from. The spawn
    # maps it onto ``Popen`` values and the stream wiring reads pipe-ness back
    # off it, so the two cannot disagree about which streams are cuprum's.
    stdio: _ResolvedStdio
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
    delivery_worker: _DeliveryWorker | None = None
    delivery_grace_s: float | None = None
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics] | None = None

    @property
    def owns_process_group(self) -> bool:
        """Whether this run owns the child's process group.

        Derived from ``process_group`` rather than stored again, so the spawn
        decision and every teardown decision are read from one field and cannot
        fall out of step. False for a child that was never spawned under
        ``OWN_GROUP``, which is also the answer for a platform where that policy
        was rejected before any child existed.
        """
        return self.process_group is ProcessGroupPolicy.OWN_GROUP

    @property
    def consumes_stdout(self) -> bool:
        """Whether the parent must consume stdout, rather than discard it.

        A redirected stdout is consumed by nobody, whatever these gates say.
        ``RunOutputOptions`` refuses that combination at construction, but only
        when *capture* or *echo* is what asks for the pipe: those two need a
        parent-side pipe to read, and a redirected stream has none. ``on_line``
        and idle observation are not refused, because they observe a pipe the
        run may still own rather than requiring one — they simply receive
        nothing from a redirected stream, which has no parent-side data to
        report. So this property is the second of the two places the
        contradiction is caught, and the one that holds for a bundle assembled
        by something other than the public constructor.

        Among the streams cuprum does hold, a registered ``on_line`` observes
        both of them, so it keeps the pipe open and the consumer running even
        when capture and echo are both off. Without that,
        ``run(output=RunOutputOptions(on_line=...))`` would attach stdout to
        ``DEVNULL`` and silently deliver nothing.

        The ``is_pipe`` test is asked first and short-circuits the rest.
        """
        return self.stdio.stdout.is_pipe and (
            self.capture
            or self.echo_stdout
            or self.idle is not None
            or self.on_line is not None
        )

    @property
    def consumes_stderr(self) -> bool:
        """Whether the parent must consume stderr, rather than discard it."""
        return self.stdio.stderr.is_pipe and (
            self.capture
            or self.echo_stderr
            or self.idle is not None
            or self.on_line is not None
        )

    @property
    def pipes(self) -> frozenset[PipeStream]:
        """The child's streams cuprum holds a parent-side pipe for.

        Delegated rather than restated: the resolved stdio already answers this,
        and a second copy of the rule can only disagree with it.
        """
        return self.stdio.pipes


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
    # uptime rather than seconds since this command started. Stamping the bundle
    # reads no clock, so it does not disturb the caller's sampling order.
    stamped = dc.replace(execution, started_at=started_at)
    process = await _spawn_subprocess(stamped)
    stamped.observation.emit("start", _EventDetails(pid=process.pid))
    return _StartedRun(process=process, execution=stamped)


async def _spawn_subprocess(
    execution: _SubprocessExecution,
) -> asyncio.subprocess.Process:
    """Spawn an async subprocess with the run's resolved stdio and environment.

    The config the spawn layer receives is deliberately a superset of what
    ``Popen`` consumes: ``pipes`` names the streams cuprum holds a parent-side
    pipe for, which is information the value cannot express. ``Popen`` leaves
    its own ``stdin``/``stdout``/``stderr`` as ``None`` for everything except
    ``PIPE``, so a child object cannot be asked whether cuprum owns a reader for
    it; and the value cannot be asked either, because it has already folded a
    non-consuming pipe down to ``DEVNULL`` — indistinguishable, at the child
    object, from a borrowed descriptor. Naming the pipes explicitly is what
    makes both backends agree.

    The first element is the observation's ``argv0`` rather than the command's
    catalogue name, so an executable binding reaches the child as its
    ``argv[0]`` as well as being the file that is executed. ``argv_with_program``
    cannot serve here: it always starts with ``str(program)``, which is the name
    the allowlist catalogued, not the path the binding supplied. The remaining
    arguments still come from the command, so the vector is exactly the one the
    observation describes.

    Returns
    -------
    asyncio.subprocess.Process
        The spawned child.

    Raises
    ------
    OSError
        If an owned target file cannot be opened, as raised by
        :func:`_open_owned_stdio` before the spawn call is made.
    """  # ruff: ignore[docstring-extraneous-exception] - OSError propagates from _open_owned_stdio.
    # Opening before the spawn and closing in the ``finally`` is the whole
    # lifetime of an owned descriptor: it exists for exactly as long as it takes
    # the child to inherit it. A borrowed binding carries no entry in ``opened``,
    # so it reaches the spawn without ever being recorded as something to close.
    #
    # The borrowed flush sits between the opens and the spawn for the same
    # reason the opens do: this is the last moment before the fork, and it is
    # the only moment that is adjacent to it on every path.
    opened = _open_owned_stdio(execution)
    try:
        _flush_borrowed_stdio(execution)
        return await _wait4_spawn.spawn_direct_process(
            _wait4_spawn.DirectProcessConfig(
                argv=(execution.observation.argv0, *execution.cmd.argv),
                stdin=_stdin_stdio(execution.stdio.stdin),
                stdout=_output_stdio(
                    execution.stdio.stdout,
                    opened.get(execution.stdio.stdout.stream),
                    consumes=execution.consumes_stdout,
                ),
                stderr=_output_stdio(
                    execution.stdio.stderr,
                    opened.get(execution.stdio.stderr.stream),
                    consumes=execution.consumes_stderr,
                ),
                pipes=execution.pipes,
                env=_merge_env(execution.ctx.env, execution.ctx.env_mode),
                cwd=_cwd_arg(execution.ctx.cwd),
                # Raises before spawning when the platform cannot honour the
                # policy, so the failure names the option rather than arriving as
                # a run that quietly contained nothing.
                **_ownership_spawn_kwargs(execution.process_group),
            )
        )
    finally:
        _close_owned_stdio(opened)


__all__ = [
    "_StartedRun",
    "_SubprocessExecution",
    "_close_owned_stdio",
    "_flush_borrowed_stdio",
    "_open_owned_stdio",
    "_output_stdio",
    "_spawn_subprocess",
    "_start_owned_process",
    "_stdin_stdio",
]
