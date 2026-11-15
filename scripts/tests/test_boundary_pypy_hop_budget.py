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
