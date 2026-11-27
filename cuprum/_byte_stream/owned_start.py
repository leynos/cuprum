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

import typing as typ

from cuprum._byte_stream.handle import RunningBytesCommand
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


class _OwnedBytesStart:
    """Mixin contributing ``SafeCmd.start_bytes``.

    ``__slots__`` is declared empty rather than omitted so the un-slotted mixin
    cannot donate a ``__dict__`` to the slotted dataclass that inherits it, for
    the same reason ``_OwnedStart`` does.
    """

    __slots__ = ()

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
        :meth:`RunningBytesCommand.wait` for the byte-exact outcome, iterate
        ``handle.stdout`` and ``handle.stderr`` for live output, or stop the
        child with the handle's control operations.

        Each reader is lossless while it is live: a consumer that stops reading
        applies backpressure to the child rather than losing bytes. Once
        settlement begins, bytes that do not fit a queue are discarded from the
        reader while capture keeps them, and the reader reports
        ``TRUNCATED_BY_SETTLEMENT``. Consume to end-of-stream before awaiting
        the result when the two must agree.

        Capture is always on and always byte-exact. A per-stream ``False``
        value inherits the forced-on shorthand, while an explicit capture
        policy still controls how that stream is retained. Line observation and
        non-pipe output targets are refused before a child exists: the former
        would give one stream two contradictory contracts, and the latter gives
        a handle no parent-side pipe to read.

        Unlike ``run()``, no deadline is enforced while the handle is merely
        held: the clock a timeout is measured against starts at the ``wait``
        that asks for one. An explicit *timeout* here is therefore the default
        deadline for every ``wait()`` on this handle rather than a limit on how
        long the child may live.

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
        options = _bytes_output(output)
        _validate_bytes_output(options)
        _reject_redirected_handle_streams(
            options,
            entry_point="SafeCmd.start_bytes()",
        )
        # Built before the spawn so the queues exist by the time the first
        # chunk is read, and so a bad bound fails before a child exists.
        streams = build_byte_streams(max_buffered_bytes)
        state = _ExecutionState(
            context=ctx,
            # Capture is forced on for the same reason a text handle forces it:
            # a handle exists so its caller can inspect the child after the
            # fact. The mode is set here, once, and travels on the execution
            # bundle to the drain and the event's capture mode.
            output=_with_capture(options),
            stdin_data=_resolve_stdin_source(stdin, ctx),
            timeout=_resolve_timeout(timeout=timeout, context=context),
            capture_bytes=True,
        )
        run = await _spawn_owned_run(cmd, state, live=streams.sinks)
        return RunningBytesCommand(run, streams)
