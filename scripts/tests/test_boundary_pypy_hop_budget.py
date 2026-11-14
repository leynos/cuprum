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
from scripts.tests.boundary_pypy_transfer_support import (
    connection_factory,
    fake_environment,
    manual_clock,
    redirect_response,
    streaming_response,
)

if typ.TYPE_CHECKING:
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
