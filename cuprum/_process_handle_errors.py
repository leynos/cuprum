"""Failures an owned running command reports to its caller.

``RunningCommand.signal`` is the one operation on the handle that the operating
system can refuse for reasons the caller has to act on, so its two refusals get
types of their own rather than surfacing as a bare ``ProcessLookupError``. Each
carries the facts a caller needs to decide what to do next — how the child
ended, or why its platform cannot take the request — so a caller never has to
reconstruct them by re-reading the handle.

``LiveStreamError`` is the byte-stream counterpart: a live reader or the
supervisor behind it failed, so the stream cannot report a clean closure. It
lives beside the refusals rather than in the byte package because a caller
imports it from ``cuprum.sh`` and the public facade must not have to import the
byte package's internals to re-export it.

They live in their own leaf module because ``cuprum.sh`` re-exports them: the
package's public exceptions are importable from the facade even though their
defining module is private, the same arrangement ``cuprum._line_iteration``
gives ``LineStream``.

The platform predicate sits here, beside the error it guards, so the answer to
"can this platform deliver that signal?" cannot drift away from the error that
reports when it cannot.
"""

import os

from cuprum.byte_streams import StreamClosure
from cuprum.echo_events import EchoStream

__all__ = [
    "LiveStreamError",
    "SignalAfterExitError",
    "UnsupportedSignalError",
    "_arbitrary_signals_supported",
]


def _arbitrary_signals_supported() -> bool:
    """Return whether this platform can deliver an arbitrary POSIX signal.

    The Windows proactor transport that asyncio uses for subprocesses accepts
    only its own terminate and kill pairs; a caller-named signal number has no
    route to the child. Everywhere else, ``send_signal`` is ``os.kill``.

    Returns
    -------
    bool
        Whether ``RunningCommand.signal`` can deliver a caller-named signal.
    """
    return os.name != "nt"


class SignalAfterExitError(ProcessLookupError):
    """Raised when a signal is requested for a child that has already exited.

    A caller coordinating several signals cannot avoid racing the child's own
    exit, so this is an ordinary outcome rather than a misuse: the handle
    reports it with the exit code it recorded, letting the caller decide
    whether the command's ending already satisfies them.

    Subclasses :class:`ProcessLookupError` because that is exactly what the
    operating system reports once the child is gone, and a caller that already
    tolerates a lost process keeps working when the handle names the condition.

    Attributes
    ----------
    exit_code : int
        The code the handle recorded for the child, which is the signal number
        negated when a signal ended it.
    pid : int | None
        The child's process identifier, or ``None`` when asyncio never reported
        one.

    """

    def __init__(self, exit_code: int, pid: int | None = None) -> None:
        """Record how the child ended and which process it was."""
        super().__init__(
            f"Cannot signal PID {pid}: it exited with code {exit_code}",
        )
        self.exit_code = exit_code
        self.pid = pid


class LiveStreamError(RuntimeError):
    """Raised when a live byte stream fails before it reaches a clean end.

    The failure belongs to one stream: the drain or the supervisor that feeds
    it raised, so the reader cannot report the closure it would have reported
    for an ordinary end. The original exception is chained as ``__cause__``, so
    a caller can inspect it without the public message having to repeat it.

    A reader whose queue publishes
    :attr:`~cuprum.byte_streams.StreamClosure.FAILED` raises this from both
    iteration and ``wait_eof()``. The other stream is unaffected unless the
    whole run failed; it settles as
    :attr:`~cuprum.byte_streams.StreamClosure.TRUNCATED_BY_SETTLEMENT` when the
    run ends because of this failure.

    Attributes
    ----------
    stream : EchoStream
        The stream whose drain or supervisor failed.
    closure : StreamClosure
        Always :attr:`~cuprum.byte_streams.StreamClosure.FAILED`. The field
        exists so a caller that catches the error does not have to infer the
        terminal status from the exception type.

    Examples
    --------
    >>> error = LiveStreamError(EchoStream.STDERR)
    >>> error.stream.value
    'stderr'
    >>> error.closure.value
    'failed'
    """

    def __init__(
        self,
        stream: EchoStream,
        *,
        cause: BaseException | None = None,
    ) -> None:
        """Record the failed stream and chain *cause* when one was supplied."""
        super().__init__(f"live {stream.value} stream failed")
        self.stream = stream
        self.closure = StreamClosure.FAILED
        if cause is not None:
            self.__cause__ = cause


class UnsupportedSignalError(RuntimeError):
    """Raised when the platform cannot deliver a caller-named signal.

    Distinct from :class:`SignalAfterExitError` because the remedy differs: no
    retry can help here, and a caller that needs to stop a child on such a
    platform has to reach for the handle's terminate or kill operations, which
    use the platform's own signals.

    Attributes
    ----------
    signal_number : int
        The signal the caller asked to deliver.
    platform_name : str
        The platform that declined it, as :data:`sys.platform` spells it.

    """

    def __init__(self, signal_number: int, platform_name: str) -> None:
        """Record the refused signal and the platform that refused it."""
        super().__init__(
            f"Signal {signal_number} cannot be delivered on this platform "
            f"({platform_name}); only POSIX signal delivery is supported",
        )
        self.signal_number = signal_number
        self.platform_name = platform_name
