"""The ``SafeCmd`` execution primitive for ``cuprum.sh``.

``SafeCmd`` is the typed, immutable curated command. Pipeline composition
lives in :mod:`cuprum.sh.pipeline`, which imports ``SafeCmd`` for its stage
type; ``SafeCmd.__or__`` resolves ``Pipeline`` with a function-local import so
that the dependency stays one-way. The ``cuprum.sh`` package re-exports both,
so callers can keep importing them from one place. ``SafeCmdBuilder`` lives in
``cuprum/sh/builder.py``, which imports ``SafeCmd`` from here; the reverse
import would close that cycle.
"""

# No ``from __future__ import annotations`` here: the public signatures are
# introspected with ``typing.get_type_hints``, so annotations are evaluated
# eagerly and every name they use is a genuine runtime import. Only the forward
# references to ``SafeCmd`` and ``Pipeline`` inside their own class bodies are
# quoted.
import asyncio
import dataclasses as dc
import typing as typ

from cuprum._command_internals import (
    _build_subprocess_execution,
    _ExecutionState,
    _finalize_prepared_command_failure,
    _prepare_execution_observation,
    _run_prepared_command,
)
from cuprum._command_prepare import _prepare_owned_command
from cuprum._execution_tracking import _ExecutionTracking
from cuprum._line_iteration import LineStream, _iter_line_events
from cuprum._pipeline_internals import _collect_hooks, _enforce_allowlist
from cuprum._process_handle import RunningCommand
from cuprum._process_handle.tasks import (
    _build_handle_execution,
    _HandleRun,
    _start_handle_run,
)
from cuprum._sink_lifecycle import _SinkBracket
from cuprum._subprocess_context import _resolve_timeout
from cuprum.catalogue import ProjectSettings
from cuprum.context import current_context
from cuprum.program import Program
from cuprum.sh.execution import ExecutionContext, StdinInput
from cuprum.sh.output import RunOutputOptions
from cuprum.sh.results import CommandResult

if typ.TYPE_CHECKING:
    # ``Pipeline`` imports ``SafeCmd`` for its stage type, so naming it at
    # runtime here would close a cycle. The annotations are strings that
    # ``typing.get_type_hints`` never resolves for this pair, and every module
    # that holds a built ``Pipeline`` has already imported it for real.
    from cuprum.sh.pipeline import Pipeline

__all__ = [
    "SafeCmd",
]


@dc.dataclass(frozen=True, slots=True)
class SafeCmd:
    """Typed representation of a curated command ready for execution."""

    program: Program

    argv: tuple[str, ...]

    project: ProjectSettings

    __weakref__: object = dc.field(
        init=False,
        repr=False,
        hash=False,
        compare=False,
    )

    @property
    def argv_with_program(self) -> tuple[str, ...]:
        """The program name followed by this command's arguments.

        Returns
        -------
        tuple[str, ...]
            An argument vector whose first item is ``str(program)``.
        """
        return (str(self.program), *self.argv)

    def __or__(self, other: "SafeCmd | Pipeline") -> "Pipeline":
        """Compose this command with another stage, producing a Pipeline."""
        # Imported here, not at module scope, because ``Pipeline`` imports
        # ``SafeCmd`` for its stage type. Deferring this side of the pair keeps
        # the dependency one-way; the modules are already in ``sys.modules`` by
        # the time any ``|`` is evaluated, so this costs a dictionary lookup.
        from cuprum.sh.pipeline import Pipeline

        return Pipeline.concat(self, other)

    async def run(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # ExecutionContext also supplies the timeout.
        context: ExecutionContext | None = None,
        stdin: StdinInput | None = None,
    ) -> CommandResult:
        """Execute the command asynchronously with predictable cancellation.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings. Its 64 KiB default bounds each mirrored
            line without affecting capture; set ``max_echo_line_bytes=None``
            for unbounded mirroring.
        timeout : float | None, default=None
            Maximum execution time in seconds. An explicit value overrides the
            timeout in ``context``.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.
        stdin : StdinInput | None, default=None
            Optional bytes or text supplied to the child process's stdin.

        Returns
        -------
        CommandResult
            The command outcome, including complete captured streams when
            ``output.capture`` is true.

        Raises
        ------
        PermissionError
            If the command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.
        UnicodeEncodeError
            If text stdin cannot be encoded by the execution context.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through execution helpers
        out = output or RunOutputOptions()
        ctx = context or ExecutionContext()
        _enforce_allowlist(self)
        stdin_data = stdin.resolve(ctx) if stdin is not None else None
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        return await _run_prepared_command(
            self,
            _ExecutionState(
                context=ctx,
                output=out,
                stdin_data=stdin_data,
                timeout=effective_timeout,
            ),
        )

    def lines(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinInput | None = None,
    ) -> LineStream:
        """Iterate the command's output lines as they arrive.

        Line events are delivered in arrival order per stream, stamped with
        monotonic seconds since the command started. Capture and echo stay
        governed by *output* independently: iterating lines does not disable
        either unless the caller asks.

        Parameters
        ----------
        output:
            Optional ``RunOutputOptions`` controlling stdout/stderr handling.
        timeout:
            Optional wall-clock timeout in seconds; ``None`` disables timeouts.
            Expiry terminates the subprocess exactly as ``run()`` does.
        context:
            Optional execution settings such as env, cwd, and cancel grace.
        stdin:
            Optional ``StdinInput`` data to feed to the subprocess.

        Returns
        -------
        LineStream
            An async iterator of ``LineEvent`` whose ``result`` attribute
            holds the final ``CommandResult`` once iteration completes.

        Raises
        ------
        ForbiddenProgramError
            If the program is not permitted by the active context allowlist.
        TimeoutExpired
            If *timeout* elapses before the command completes.
        UnicodeEncodeError
            If ``stdin`` text cannot be encoded with the context's encoding.
        """  # ruff: ignore[docstring-extraneous-exception] - all propagate from allowlist, timeout, and stdin encode
        out = output or RunOutputOptions()
        ctx = context or ExecutionContext()
        _enforce_allowlist(self)
        stdin_data = stdin.resolve(ctx) if stdin is not None else None
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        tracking = _ExecutionTracking(
            execution_hooks=_collect_hooks(current_context()),
            pending_tasks=[],
            # Line iteration never opens a presentation session: the line
            # events are the caller's own consumption of the streams, so there
            # is no adapter framing to bracket. The empty bracket keeps the
            # required field satisfied.
            sink_bracket=_SinkBracket(None),
        )
        observation = _prepare_execution_observation(self, ctx, tracking, out)

        return LineStream(
            _iter_line_events(
                _build_subprocess_execution(
                    self,
                    _ExecutionState(
                        context=ctx,
                        output=out,
                        stdin_data=stdin_data,
                        timeout=effective_timeout,
                    ),
                    observation=observation,
                ),
                tracking,
            ),
        )

    async def start(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # ExecutionContext also supplies the timeout.
        context: ExecutionContext | None = None,
        stdin: StdinInput | None = None,
    ) -> RunningCommand:
        """Start the command and return an owned handle to it.

        The child is spawned and its PID announced before this returns; the
        caller decides what happens next. Await :meth:`RunningCommand.wait` for
        the outcome, deliver a signal with :meth:`RunningCommand.signal`, or
        stop the child with :meth:`RunningCommand.terminate` or
        :meth:`RunningCommand.kill`.

        The handle owns the child's lifetime. Using it as an ``async with``
        block settles the run on exit — a child still running is stopped down
        the same ``SIGTERM``, cancel-grace, ``SIGKILL`` path a cancelled
        ``run()`` takes — so a caller cannot leave a child behind by forgetting
        about it. Output is captured unless *output* says otherwise, so a
        handle that is not iterated still reports what the command wrote.

        Unlike ``run()``, no deadline is enforced while the handle is merely
        held: the clock a timeout is measured against starts at the ``wait``
        that asks for one. An explicit *timeout* here is therefore the default
        deadline for every ``wait()`` on this handle rather than a limit on how
        long the child may live.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings. Capture defaults to on, so the handle's
            result carries the command's streams.
        timeout : float | None, default=None
            Default maximum time a ``wait()`` on this handle may take, in
            seconds. An explicit value overrides the timeout in ``context``.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks, encoding, and the cancel
            grace a graceful stop allows.
        stdin : StdinInput | None, default=None
            Optional bytes or text supplied to the child process's stdin.

        Returns
        -------
        RunningCommand
            An owned handle to the running command.

        Raises
        ------
        PermissionError
            If the command is not allowed by the active scope.
        UnicodeEncodeError
            If text stdin cannot be encoded by the execution context.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate from the allowlist and stdin resolution
        _enforce_allowlist(self)
        ctx = context or ExecutionContext()
        state = _ExecutionState(
            context=ctx,
            # Capture is forced on: a handle exists so its caller can inspect
            # the child after the fact, and a child whose streams were routed
            # to ``DEVNULL`` cannot be asked what it said. The caller's own
            # settings still govern echo and every other presentation choice.
            output=_with_capture(output),
            stdin_data=stdin.resolve(ctx) if stdin is not None else None,
            timeout=_resolve_timeout(timeout=timeout, context=context),
        )
        return RunningCommand(await _spawn_owned_run(self, state))

    def run_sync(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinInput | None = None,
    ) -> CommandResult:
        """Execute the command synchronously.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings. The default limits each mirrored line to
            64 KiB; ``max_echo_line_bytes=None`` restores unbounded echoing
            while preserving the same capture contract.
        timeout : float | None, default=None
            Maximum execution time in seconds.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.
        stdin : StdinInput | None, default=None
            Optional bytes or text supplied to the child process's stdin.

        Returns
        -------
        CommandResult
            The command outcome, including complete captured streams when
            enabled.

        Raises
        ------
        PermissionError
            If the command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.
        UnicodeEncodeError
            If text stdin cannot be encoded by the execution context.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through run()
        return asyncio.run(
            self.run(output=output, timeout=timeout, context=context, stdin=stdin),
        )


def _with_capture(output: RunOutputOptions | None) -> RunOutputOptions:
    """Return *output*, or the default, with capture forced on.

    Capture is forced on for a handle because a handle exists so its caller can
    inspect the child after the fact, and a child whose streams were routed to
    ``DEVNULL`` cannot be asked what it said. The caller's own settings still
    govern echo and every other presentation decision.

    Returns
    -------
    RunOutputOptions
        The caller's settings with ``capture`` set, or the default settings
        when none were given.
    """
    resolved = output or RunOutputOptions()
    return resolved if resolved.capture else dc.replace(resolved, capture=True)


async def _spawn_owned_run(cmd: SafeCmd, state: _ExecutionState) -> _HandleRun:
    """Prepare *cmd*, spawn its child, and hand back the owned record.

    The spawn owns its own child: ``_start_handle_run`` terminates and reaps
    anything it started before letting a failure out. What it cannot own is the
    session the execution was prepared against, so that framing is closed here,
    on the one path that can fail after preparation returned.

    Returns
    -------
    _HandleRun
        The spawned child and everything its caller needs to drive and settle
        it.

    Raises
    ------
    BaseException
        Whatever preparation or the spawn raised, once the session opened for
        the run has been finalized.
    """  # ruff: ignore[docstring-extraneous-exception] - the guard re-raises whatever the spawn raised
    prepared = await _prepare_owned_command(cmd, state)
    try:
        return await _start_handle_run(
            _build_handle_execution(prepared.execution),
            prepared.tracking,
            state.timeout,
        )
    except BaseException as run_error:
        await _finalize_prepared_command_failure(
            prepared.tracking,
            prepared.execution.observation,
            run_error,
        )
        raise
