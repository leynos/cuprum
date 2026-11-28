"""Starting a command as an owned handle with live byte streams.

``_OwnedBytesStart`` contributes ``SafeCmd.start_bytes``. The method resolves a
byte run's inputs and then reuses the text handle's preparation, spawn,
announcement, and partial-attachment reclaim: the only differences are that the
run is marked ``capture_bytes=True``, the caller's line observer and any
non-pipe output target are rejected before a child exists, a ``StdinSource`` is
accepted, and the live queues are attached to the consumers.

The rejections happen before the spawn on purpose. A line observer cannot
coexist with a byte-exact capture contract, and a redirected stream has no
parent-side pipe for a handle to read; discovering either after the child ran
would be a failure a caller could not act on.
"""

from __future__ import annotations

import functools
import typing as typ

from cuprum._byte_stream.handle import RunningBytesCommand
from cuprum._byte_stream.settlement import _stop_failed_child
from cuprum._byte_stream.wiring import build_byte_streams
from cuprum._bytes_run import _bytes_output, _validate_bytes_output
from cuprum._command_internals import _ExecutionState
from cuprum._pipeline_internals import _enforce_allowlist
from cuprum._process_handle.owned_start import (
    _reject_redirected_handle_streams,
    _spawn_owned_run,
    _with_capture,
)
from cuprum._subprocess_context import _resolve_timeout
from cuprum.sh.execution import ExecutionContext, StdinSource, _resolve_stdin_source

# The options type is a public parameter annotation, so it must remain a
# runtime name: ``typing.get_type_hints`` resolves it from this module.
from cuprum.sh.output import (
    RunOutputOptions,  # ruff: ignore[typing-only-first-party-import] - public annotation resolves at runtime
)

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd

__all__ = ["_OwnedBytesStart"]

# A 64 KiB queue per stream is large enough to hold one ordinary pipe-buffer
# read and small enough that a stalled consumer cannot retain an unbounded
# amount. It is documented on ``start_bytes``.
_DEFAULT_MAX_BUFFERED_BYTES = 64 * 1024


def _bytes_execution_state(
    ctx: ExecutionContext,
    output: RunOutputOptions | None,
    stdin: StdinSource | None,
    timeout: float | None,
) -> _ExecutionState:
    """Resolve the run inputs a byte handle spawns from.

    The option rejections happen here, before a child exists. A line observer
    cannot coexist with byte-exact capture, and a redirected stream has no
    parent-side pipe for a handle to read.

    Returns
    -------
    _ExecutionState
        The resolved inputs, with capture forced on and byte mode set.
    """
    options = _bytes_output(output)
    _validate_bytes_output(options)
    _reject_redirected_handle_streams(
        options,
        entry_point="SafeCmd.start_bytes()",
    )
    return _ExecutionState(
        context=ctx,
        # Capture is forced on for the same reason a text handle forces it: a
        # handle exists so its caller can inspect the child after the fact. The
        # mode is set here, once, and travels on the execution bundle to the
        # drain and the event's capture mode.
        output=_with_capture(options),
        stdin_data=_resolve_stdin_source(stdin, ctx),
        timeout=timeout,
        capture_bytes=True,
    )


class _OwnedBytesStart:
    """Mixin contributing ``SafeCmd.start_bytes``.

    ``__slots__`` is declared empty rather than omitted so the un-slotted mixin
    cannot donate a ``__dict__`` to the slotted dataclass that inherits it, for
    the same reason ``_OwnedStart`` does.
    """

    __slots__ = ()

    # @codescene(disable:"Excess Number of Function Arguments") The five
    # keyword-only inputs are the published contract, mirroring start() plus one
    # queue bound; grouping them would change every call site. Reassess 2027-05.
    async def start_bytes(  # ruff: ignore[too-many-arguments] - the five keyword-only inputs mirror start() plus one queue bound; bundling them would change the documented signature.
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # ExecutionContext also supplies the timeout.
        context: ExecutionContext | None = None,
        stdin: StdinSource | None = None,
        max_buffered_bytes: int = _DEFAULT_MAX_BUFFERED_BYTES,
    ) -> RunningBytesCommand:
        """Start the command and return an owned handle with live byte streams.

        The child is spawned and its PID announced before this returns. Await
        :meth:`RunningBytesCommand.wait` for the byte-exact outcome, or iterate
        ``handle.stdout`` and ``handle.stderr`` for live output.

        Each reader is lossless while it is live; settlement may truncate a
        reader whose queue cannot fit the remainder, so consume to end-of-stream
        before awaiting the result. See the users' guide for the full contract.
        Capture is always on and byte-exact; a caller-supplied ``on_line`` and
        non-pipe output targets are refused before a child exists. Unlike
        ``run()``, a *timeout* is the default deadline for every ``wait()`` on
        the handle rather than a limit on the child's life.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture, echo, and presentation settings. Capture defaults to on;
            ``on_line`` is refused.
        timeout : float | None, default=None
            Default maximum time a ``wait()`` on this handle may take, in
            seconds. An explicit value overrides the timeout in ``context``.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks, encoding, and the cancel
            grace a graceful stop allows.
        stdin : StdinSource | None, default=None
            A complete payload or a streaming producer for the child's stdin.
        max_buffered_bytes : int, default=65536
            The payload-byte bound for each stream's live queue. It also caps
            each stream's read size. Must be a positive integer.

        Returns
        -------
        RunningBytesCommand
            An owned handle whose ``stdout`` and ``stderr`` are independent
            byte readers.

        Raises
        ------
        PermissionError
            If the command is not allowed by the active scope.
        TypeError
            If *max_buffered_bytes* is not an integer, or is a ``bool``.
        ValueError
            If *max_buffered_bytes* is not positive, if stdout or stderr is
            redirected to a non-pipe target, or if *output* supplies
            ``on_line``.
        UnicodeEncodeError
            If text stdin cannot be encoded by the execution context.
        """  # ruff: ignore[docstring-extraneous-exception] - exceptions propagate from allowlist, stdin, and option validation
        cmd = typ.cast("SafeCmd", self)
        _enforce_allowlist(cmd)
        ctx = context or ExecutionContext()
        # Built before the spawn so the queues exist by the time the first
        # chunk is read, and so a bad bound fails before a child exists.
        streams = build_byte_streams(max_buffered_bytes)
        state = _bytes_execution_state(
            ctx,
            output,
            stdin,
            _resolve_timeout(timeout=timeout, context=context),
        )
        run = await _spawn_owned_run(cmd, state, live=streams.sinks)
        # Bound after the spawn because the stop needs the run; a failure that
        # arrived before this line sets its request and is started by bind().
        streams.stop.bind(functools.partial(_stop_failed_child, run))
        return RunningBytesCommand(run, streams)
