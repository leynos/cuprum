"""The PyPy deadline watchdog must stop the socket a blocked read holds.

A deadline checked only between reads cannot stop a server that trickles
header bytes, so the watchdog shuts the connection's socket down when the
transfer's own clock passes the deadline. These cases drive that behaviour
through the watchdog and through the download boundary; the loopback HTTPS
cases live in `test_boundary_pypy_https.py`.
"""

from __future__ import annotations

import socket
import threading
import typing as typ
from unittest import mock

import pytest

from scripts import pypy_download as downloader
from scripts.tests.boundary_pypy_transfer_support import (
    fake_response,
    fixed_clock,
    manual_clock,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


class TestDeadlineWatchdog:
    """The watchdog must stop the socket a close-delimited response owns."""

    def test_the_socket_is_captured_before_the_response_takes_it(self) -> None:
        """The socket is armed before `getresponse` can clear the reference."""
        stopped = threading.Event()
        sock = mock.Mock()
        sock.shutdown.side_effect = lambda _how: stopped.set()
        connection = mock.Mock()
        connection.sock = sock
        watchdog = downloader._DeadlineWatchdog(
            connection, downloader._Transfer(0.0, fixed_clock)
        )
        # A close-delimited response leaves the socket with the response and
        # clears the connection's reference, as `getresponse` does.
        connection.sock = None
        with watchdog:
            # Exiting immediately would signal `_stop` before the watchdog
            # thread runs, which legitimately skips the shutdown.
            assert stopped.wait(5.0), "the watchdog must shut the socket down"
        sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_the_watchdog_waits_on_the_transfer_clock(self) -> None:
        """The watchdog waits the remaining transfer time, then shuts down."""
        clock = manual_clock()
        clock.return_value = 4.0
        waits: list[float] = []
        sock = mock.Mock()
        connection = mock.Mock()
        connection.sock = sock

        def wait(seconds: float) -> bool:
            """Record the remaining time and report the deadline as reached."""
            waits.append(seconds)
            return False

        watchdog = downloader._DeadlineWatchdog(
            connection, downloader._Transfer(9.0, clock, wait)
        )
        with watchdog:
            pass
        assert waits == [pytest.approx(5.0)], (
            "the watchdog must wait on the transfer's own clock"
        )
        sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_a_blocked_read_is_released_by_the_injected_waiter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read blocked past the deadline fails once the injected waiter fires."""
        clock = manual_clock()
        released = threading.Event()
        waits: list[float] = []
        sock = mock.Mock()
        sock.shutdown.side_effect = lambda _how: released.set()

        def wait(seconds: float) -> bool:
            """Record the remaining time and reach the deadline without sleeping."""
            waits.append(seconds)
            clock.return_value = 1_000.0
            return False

        def read1(_size: int) -> bytes:
            """Block until the watchdog shuts the socket down, then report EOF."""
            if not released.wait(5.0):
                msg = "the watchdog never shut the blocked socket down"
                raise AssertionError(msg)
            return b""

        response = fake_response(None, [])
        response.read1.side_effect = read1
        connection = mock.Mock()
        connection.sock = sock
        connection.getresponse.return_value = response
        monkeypatch.setattr(
            downloader.http.client,
            "HTTPSConnection",
            lambda *_args, **_kwargs: connection,
        )
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader._download(
                "https://example.invalid/asset",
                tmp_path / "binary",
                clock=clock,
                wait=wait,
            )
        assert waits == [pytest.approx(downloader._TRANSFER_DEADLINE_SECONDS)], (
            "the download boundary must pass the transfer's remaining time"
        )
        sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
