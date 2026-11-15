"""Bound every redirect hop by the time the transfer has left.

Opening a connection and negotiating TLS used to run on a fixed 60 s timeout,
so a redirect chain could spend a fresh minute on each hop and outlive the one
deadline the whole transfer shares. These cases stop the clock most of the way
through that deadline and assert the budget each hop's connection was actually
opened with, including the hop that has no time left at all.
"""

from __future__ import annotations

import dataclasses
import typing as typ
from unittest import mock

import pytest

from scripts import pypy_download as downloader
from scripts import pypy_download_deadline as deadline
from scripts.tests.boundary_pypy_transfer_support import (
    connection_factory,
    fake_environment,
    manual_clock,
    redirect_response,
    streaming_response,
)

if typ.TYPE_CHECKING:
    import http.client
    from pathlib import Path


def _budgeted_environment(
    *connections: mock.Mock, clock: mock.Mock, budgets: list[float]
) -> downloader.Environment:
    """Serve `connections` in order and collect the budget each hop was opened with."""
    connect = connection_factory(*connections, timeouts=budgets)
    return dataclasses.replace(fake_environment(clock=clock), connect=connect)


#: The whole deadline with five seconds still to run, so the second hop is left
#: with a budget far below the 60 s single-hop limit.
_ALMOST_SPENT = 895.0
#: Four seconds left: enough for two phases that each fit the hop's budget.
_FOUR_LEFT = 896.0
#: The deadline itself: past this the transfer has no time left.
_SPENT = 900.0


class TestHopBudget:
    """A redirect hop may take only the time the transfer has left."""

    def test_a_redirected_hop_is_bounded_by_the_time_left(self, tmp_path: Path) -> None:
        """The first hop takes the 60 s limit; the late hop takes only 5 s."""
        clock = manual_clock()
        redirect = redirect_response("https://example.invalid/asset")

        def redirect_header(name: str) -> str | None:
            """Answer the header, leaving 5 s of the transfer's deadline."""
            clock.return_value = _ALMOST_SPENT
            return "https://example.invalid/asset" if name == "Location" else None

        redirect.getheader.side_effect = redirect_header
        first, body = mock.Mock(), mock.Mock()
        first.getresponse.return_value = redirect
        body.getresponse.return_value = streaming_response(b"payload")
        timeouts: list[float] = []
        downloader._download(
            "https://example.invalid/start",
            tmp_path / "binary",
            _budgeted_environment(first, body, clock=clock, budgets=timeouts),
        )
        assert timeouts[0] == pytest.approx(60.0), (
            "a hop with the whole deadline ahead keeps the 60 s connect limit"
        )
        assert timeouts[1] == pytest.approx(5.0), (
            "a hop inside the deadline must be bounded by the time left, not 60 s"
        )

    def test_a_hop_with_no_time_left_is_refused_before_it_connects(
        self, tmp_path: Path
    ) -> None:
        """A hop that starts exactly at the deadline opens no second connection."""
        clock = manual_clock()
        redirect = redirect_response("https://example.invalid/asset")

        def redirect_header(name: str) -> str | None:
            """Answer the header, spending the deadline exactly."""
            clock.return_value = _SPENT
            return "https://example.invalid/asset" if name == "Location" else None

        redirect.getheader.side_effect = redirect_header
        first = mock.Mock()
        first.getresponse.return_value = redirect
        timeouts: list[float] = []
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader._download(
                "https://example.invalid/start",
                tmp_path / "binary",
                _budgeted_environment(first, clock=clock, budgets=timeouts),
            )
        assert timeouts == [pytest.approx(60.0)], (
            "a hop with no time left must be refused, not handed a socket"
        )

    def test_connection_setup_counts_against_the_deadline(self, tmp_path: Path) -> None:
        """A hop that spends the transfer's time connecting reads no body."""
        clock = manual_clock()
        connection = mock.Mock()

        def connect(
            host: str, port: int | None, *, timeout: float
        ) -> http.client.HTTPSConnection:
            """Spend the whole deadline inside the connection setup."""
            clock.return_value = _SPENT
            return typ.cast("http.client.HTTPSConnection", connection)

        environment = dataclasses.replace(
            fake_environment(clock=clock), connect=connect
        )
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader._download(
                "https://example.invalid/asset", tmp_path / "binary", environment
            )
        assert connection.getresponse.call_count == 0, (
            "connection setup is part of the hop's budget, so a transfer whose "
            "deadline passed while connecting must not go on to read a body"
        )

    def test_the_request_is_bounded_by_the_time_left_after_connecting(
        self, tmp_path: Path
    ) -> None:
        """Connecting spends part of the hop; the request gets what remains."""
        clock = manual_clock()
        connection = mock.Mock()
        sent: list[float] = []
        connection.sock.settimeout.side_effect = sent.append
        connection.getresponse.return_value = streaming_response(b"payload")

        def connect() -> None:
            """Spend five of the hop's sixty seconds connecting."""
            clock.return_value = _ALMOST_SPENT

        connection.connect.side_effect = connect
        downloader._download(
            "https://example.invalid/asset",
            tmp_path / "binary",
            _budgeted_environment(connection, clock=clock, budgets=[]),
        )
        assert sent == [pytest.approx(5.0)], (
            "a socket timeout bounds one call, so the request must be sent "
            "under the time left after connecting, not the hop's first budget"
        )

    def test_connection_setup_that_spends_the_deadline_sends_no_request(
        self, tmp_path: Path
    ) -> None:
        """A connection that finishes after the deadline sends no request."""
        clock = manual_clock()
        connection = mock.Mock()

        def connect() -> None:
            """Spend the whole deadline inside the connection setup."""
            clock.return_value = _SPENT

        connection.connect.side_effect = connect
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader._download(
                "https://example.invalid/asset",
                tmp_path / "binary",
                _budgeted_environment(connection, clock=clock, budgets=[]),
            )
        assert connection.request.call_count == 0, (
            "connecting is part of the hop's budget, so a setup that spends "
            "the deadline must not go on to send a request"
        )
        assert connection.sock.settimeout.call_count == 0, (
            "a spent transfer must not arm a socket for its request"
        )

    def test_phases_that_each_fit_the_budget_still_share_one_deadline(
        self, tmp_path: Path
    ) -> None:
        """Connecting and requesting each inside the hop still meet the deadline."""
        clock = manual_clock()
        redirect = redirect_response("https://example.invalid/asset")

        def redirect_header(name: str) -> str | None:
            """Leave four seconds of the transfer's deadline."""
            clock.return_value = _FOUR_LEFT
            return "https://example.invalid/asset" if name == "Location" else None

        redirect.getheader.side_effect = redirect_header
        first, last = mock.Mock(), mock.Mock()
        first.getresponse.return_value = redirect
        sent: list[float] = []

        def open_connection() -> None:
            """Spend three of the four seconds the hop has left."""
            clock.return_value = _SPENT - 1.0

        def send_request(*_args: object, **_kwargs: object) -> None:
            """Spend the rest of the deadline sending the request."""
            clock.return_value = _SPENT + 0.5

        last.connect.side_effect = open_connection
        last.sock.settimeout.side_effect = sent.append
        last.request.side_effect = send_request
        # A body the transfer could have read makes the failure below the
        # deadline's, not a mock's missing context manager.
        last.getresponse.return_value = streaming_response(b"payload")
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader._download(
                "https://example.invalid/start",
                tmp_path / "binary",
                _budgeted_environment(first, last, clock=clock, budgets=[]),
            )
        assert sent == [pytest.approx(1.0)], (
            "each phase fits the hop's budget, so only the recomputed time left "
            "keeps their total inside the transfer's one deadline"
        )
        assert last.getresponse.call_count == 0, (
            "the request outlived the deadline, so no body is read"
        )


class TestSingleClockReading:
    """One reading both refuses a spent transfer and sizes the hop's budget."""

    def test_the_budget_is_sized_from_one_clock_reading(self) -> None:
        """A second reading could pass the guard and then budget zero seconds."""
        clock = mock.Mock(side_effect=[100.0, 830.0])
        transfer = deadline._Transfer(_SPENT, clock)
        assert deadline._hop_timeout(transfer) == pytest.approx(60.0), (
            "a hop with 800 s left keeps the 60 s connect limit"
        )
        assert clock.call_count == 1, (
            "the budget must come from the same reading that refused or allowed "
            "the hop, so a deadline passing between two readings cannot yield a "
            "zero timeout"
        )

    def test_a_spent_transfer_is_refused_from_that_same_reading(self) -> None:
        """A clock at the deadline is refused rather than yielding a zero timeout."""
        clock = mock.Mock(return_value=_SPENT)
        transfer = deadline._Transfer(_SPENT, clock)
        with pytest.raises(TimeoutError, match="transfer deadline"):
            deadline._hop_timeout(transfer)
        assert clock.call_count == 1, (
            "the refusal must come from the reading the budget would have used"
        )
