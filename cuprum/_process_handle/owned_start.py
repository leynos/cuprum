"""Starting a command as an owned handle, and the state that start resolves.

``SafeCmd.start()`` resolves a different set of run inputs from ``run()``: the
capture flag is forced on, and an explicit deadline becomes the default for
every ``wait()`` on the handle rather than a limit on the child's life. Keeping
that resolution here, next to the handle it produces, means the ``_process_handle``
package owns the whole answer to "what does a handle start from?" instead of
borrowing half of it from the ``SafeCmd`` module.

``_OwnedStart`` contributes ``SafeCmd.start``. The ``SafeCmd`` module is at the
repository's 400-line module ceiling, so the method lives where its subject
does; the class inherits it and the public surface is unchanged, because
``SafeCmd.start`` still resolves as an ordinary bound method whose annotations
``typing.get_type_hints`` reads from this module's globals.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum._command_internals import (
    _ExecutionState,
    _finalize_prepared_command_failure,
)
from cuprum._command_prepare import _prepare_owned_command
from cuprum._pipeline_internals import _enforce_allowlist
from cuprum._process_handle import RunningCommand
from cuprum._process_handle.tasks import (
    _build_handle_execution,
    _HandleRun,
    _start_handle_run,
)
from cuprum._subprocess_context import _resolve_timeout
from cuprum.sh.execution import ExecutionContext, StdinInput
from cuprum.sh.output import RunOutputOptions

if typ.TYPE_CHECKING:
    from cuprum.capture_policy import CapturePolicy
    from cuprum.sh import SafeCmd


def _reject_redirected_handle_streams(output: RunOutputOptions) -> None:
    """Reject streams a running handle cannot read from parent-side pipes.

    Raises
    ------
    ValueError
        If stdout or stderr names a non-pipe target.
    """
    redirected = [
        name
        for name, target in (("stdout", output.stdout), ("stderr", output.stderr))
        if target is not None and target.kind != "pipe"
    ]
    if redirected:
        msg = (
            "SafeCmd.start() always captures output, so "
            f"{', '.join(redirected)} cannot be redirected. "
            "Use run() or run_sync() to redirect streams."
        )
        raise ValueError(msg)


def _handle_capture_setting(
    *,
    setting: CapturePolicy | bool | None,
) -> CapturePolicy | bool | None:
    """Make an explicit false inherit the handle's forced-on capture flag."""
    return None if setting is False else setting


def _handle_capture_options(output: RunOutputOptions) -> RunOutputOptions:
    """Enable handle capture while retaining each explicit capture policy."""
    capture_stdout = _handle_capture_setting(setting=output.capture_stdout)
    capture_stderr = _handle_capture_setting(setting=output.capture_stderr)
    capture_settings_unchanged = (
        capture_stdout is output.capture_stdout
        and capture_stderr is output.capture_stderr
    )
    if output.capture and capture_settings_unchanged:
        return output
    return dc.replace(
        output,
        capture=True,
        capture_stdout=capture_stdout,
        capture_stderr=capture_stderr,
    )


def _with_capture(output: RunOutputOptions | None) -> RunOutputOptions:
    """Return *output*, or the default, with capture forced on.

    Capture is forced on for a handle because a handle exists so its caller can
    inspect the child after the fact, and a child whose streams were routed to
    ``DEVNULL`` cannot be asked what it said. The caller's own settings still
    govern echo and every other presentation decision. A per-stream ``False``
    value is reset to ``None`` so it inherits the forced-on shorthand, while
    an explicit capture policy remains in force.

    Owned handles read both streams through parent-side pipes, so a non-pipe
    target is refused before the capture fields are normalized.

    Returns
    -------
    RunOutputOptions
        The caller's settings with ``capture`` set and per-stream opt-outs
        removed, or the default settings when none were given. Explicit
        per-stream capture policies remain unchanged.

    """
    resolved = output or RunOutputOptions()
    _reject_redirected_handle_streams(resolved)
    return _handle_capture_options(resolved)


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
            execution=prepared.execution,
        )
        raise


class _OwnedStart:
    """Mixin contributing ``SafeCmd.start`` and the spawn behind it.

    ``__slots__`` is declared empty rather than omitted so the un-slotted mixin
    cannot donate a ``__dict__`` to the slotted dataclass that inherits it. It
    is deliberately *not* a dataclass: :func:`dataclasses.dataclass` refuses a
    base that already declares ``__slots__``, and this class needs no fields of
    its own.
    """

    __slots__ = ()

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
        about it. Output capture is always on, whatever *output* says. A
        per-stream ``False`` value inherits the forced-on shorthand, while an
        explicit capture policy still controls that stream's retention.

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
        ValueError
            If stdout or stderr is redirected to a non-pipe target.
        UnicodeEncodeError
            If text stdin cannot be encoded by the execution context.
        """  # ruff: ignore[docstring-extraneous-exception] - exceptions propagate from allowlist, stdin, and owned-capture validation
        cmd = typ.cast("SafeCmd", self)
        _enforce_allowlist(cmd)
        ctx = context or ExecutionContext()
        state = _ExecutionState(
            context=ctx,
            # Capture is forced on: a handle exists so its caller can inspect
            # the child after the fact, and a child whose streams were routed
            # to ``DEVNULL`` cannot be asked what it said. Explicit policies
            # still govern each stream's retention; presentation stays with
            # the caller.
            output=_with_capture(output),
            stdin_data=stdin.resolve(ctx) if stdin is not None else None,
            timeout=_resolve_timeout(timeout=timeout, context=context),
        )
        return RunningCommand(await _spawn_owned_run(cmd, state))


__all__ = ["_OwnedStart", "_spawn_owned_run", "_with_capture"]
