"""The one transfer deadline and the watchdog that enforces it.

A deadline covers every redirect hop, the header parse, and every body read.
A deadline checked only between reads cannot stop a server that trickles
header bytes, because each individual read still finishes inside the socket
timeout. The watchdog therefore shuts the connection's socket down once the
transfer's own clock says no time remains, which makes a blocked read fail.

The clock and the waiter are seams: a test supplies both together, so a
deadline is reached and a blocked read released without real seconds.
"""

from __future__ import annotations

import contextlib
import dataclasses
import socket
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import http.client

    type Clock = cabc.Callable[[], float]
    type Waiter = cabc.Callable[[float], bool]


@dataclasses.dataclass(frozen=True, slots=True)
class _Transfer:
    """The one deadline, clock, and wait every redirect hop of a download shares.

    A test supplies `wait` beside the clock it advances, so one injected time
    source covers the deadline check and the wait that releases a blocked read.
    """

    deadline: float
    clock: Clock
    wait: Waiter | None = None

    def expired(self) -> bool:
        """Report whether the transfer has outlived its deadline."""
        return self.clock() > self.deadline

    def remaining(self) -> float:
        """Report the seconds left before the deadline, never below zero."""
        return max(self.deadline - self.clock(), 0.0)


def _require_time_left(transfer: _Transfer) -> None:
    """Refuse to continue a transfer once its deadline has passed."""
    if transfer.expired():
        msg = "release download exceeded the transfer deadline"
        raise TimeoutError(msg)


class _DeadlineWatchdog:
    """Shut a connection's socket down when its transfer deadline passes.

    The socket is captured when the watchdog is armed, because
    `HTTPConnection.getresponse` hands a close-delimited response its socket
    and clears `connection.sock`. Reading it at the deadline would find
    nothing and leave a stalled body blocked until the socket timeout.
    """

    def __init__(
        self,
        connection: http.client.HTTPSConnection,
        transfer: _Transfer,
    ) -> None:
        """Arm a watchdog for `connection`'s current socket.

        The remaining time comes from the transfer's own clock, so the wait and
        every deadline check share one time source. A transfer that carries a
        test's waiter advances that clock instead of blocking for the remaining
        real seconds.
        """
        self._sock = connection.sock
        self._transfer = transfer
        self._stop = threading.Event()
        self._wait = self._stop.wait if transfer.wait is None else transfer.wait
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def __enter__(self) -> _DeadlineWatchdog:
        """Start watching, so a blocked read fails at the deadline."""
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop watching and wait for the watchdog thread to finish."""
        self._stop.set()
        self._thread.join()

    def _watch(self) -> None:
        """Wait until the transfer has no time left, then shut the socket down.

        The clock is re-read after each wait returns, so a wakeup that arrives
        before the deadline only starts another wait; the socket is shut down
        only once the transfer's own clock says no time remains. Without that
        re-check a coarse or injected waiter could interrupt a healthy read.
        """
        while True:
            remaining = self._transfer.remaining()
            if remaining <= 0.0:
                break
            if self._wait(remaining):
                return
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)
