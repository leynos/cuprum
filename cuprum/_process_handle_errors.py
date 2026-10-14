"""Signal-delivery failures for an owned running command.

``RunningCommand.signal`` is the one operation on the handle that the operating
system can refuse for reasons the caller has to act on, so its two refusals get
types of their own rather than surfacing as a bare ``ProcessLookupError``. Each
carries the facts a caller needs to decide what to do next — how the child
ended, or why its platform cannot take the request — so a caller never has to
reconstruct them by re-reading the handle.

They live in their own leaf module because ``cuprum.sh`` re-exports them: the
package's public exceptions are importable from the facade even though their
defining module is private, the same arrangement ``cuprum._line_iteration``
gives ``LineStream``.

The platform predicate sits here, beside the error it guards, so the answer to
"can this platform deliver that signal?" cannot drift away from the error that
reports when it cannot.
"""

from __future__ import annotations

import os

__all__ = [
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
