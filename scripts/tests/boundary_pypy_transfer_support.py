"""Shared test doubles for the bounded PyPy download contracts.

Each helper builds a fake response, clock, or connection sequence so the
transfer tests run without a socket, a network call, or a real-time wait. The
download takes all three through `Environment`, so these doubles are injected
at the public boundary rather than patched into the module.
"""

from __future__ import annotations

import http.client
import time
import typing as typ
from unittest import mock

from scripts import pypy_download as downloader
from scripts.pypy_download_metrics import LOGGER, LoggingMetrics

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import logging
    from pathlib import Path

    from scripts.pypy_download_metrics import MetricsSink


def fake_response(declared: str | None, chunks: cabc.Iterable[bytes]) -> mock.MagicMock:
    """Build a 200 response whose `read1` yields `chunks` and then EOF."""
    fake = mock.MagicMock()
    fake.__enter__.return_value = fake
    fake.status = http.client.OK
    fake.getheader.side_effect = lambda name: (
        declared if name == "Content-Length" else None
    )
    fake.read1.side_effect = [*chunks, b""]
    return fake


def streaming_response(body: bytes, declared: str | None = None) -> mock.MagicMock:
    """Build a 200 response that streams `body` and reports `declared`."""
    return fake_response(declared, [body])


def redirect_response(location: str) -> mock.MagicMock:
    """Build a 302 response whose `Location` header points at `location`."""
    fake = mock.MagicMock()
    fake.__enter__.return_value = fake
    fake.status = 302
    fake.getheader.side_effect = lambda name: location if name == "Location" else None
    return fake


def connection_factory(*fakes: mock.Mock) -> downloader.Connect:
    """Return a connection factory that hands out `fakes` in order, once each."""
    remaining = iter(fakes)

    def connect(*_args: object, **_kwargs: object) -> http.client.HTTPSConnection:
        """Hand out the next prepared connection."""
        return typ.cast("http.client.HTTPSConnection", next(remaining))

    return connect


class RecordingMetrics:
    """Collect every metric sample a download reports, in order."""

    def __init__(self) -> None:
        """Start with no samples."""
        self.counters: list[tuple[str, float, dict[str, str]]] = []
        self.histograms: list[tuple[str, float, dict[str, str]]] = []

    def inc_counter(
        self, name: str, value: float, labels: cabc.Mapping[str, str]
    ) -> None:
        """Record one counter sample."""
        self.counters.append((name, value, dict(labels)))

    def observe_histogram(
        self, name: str, value: float, labels: cabc.Mapping[str, str]
    ) -> None:
        """Record one histogram sample."""
        self.histograms.append((name, value, dict(labels)))

    def outcomes(self) -> list[str]:
        """Return the outcome label of every counter sample."""
        return [labels["outcome"] for _, _, labels in self.counters]

    def observed(self, name: str) -> list[float]:
        """Return every value observed for `name`."""
        return [value for metric, value, _ in self.histograms if metric == name]


def fake_environment(
    *fakes: mock.Mock,
    clock: cabc.Callable[[], float] = time.monotonic,
    sink: MetricsSink | None = None,
    logger: logging.Logger | None = None,
) -> downloader.Environment:
    """Build an environment whose connections come from `fakes` in order.

    The sink and logger default to the same ones the bootstrap uses, so a case
    that inspects log records needs to supply nothing.

    Returns
    -------
    downloader.Environment
        An environment that serves each hop from the next fake in `fakes` and
        reports through `sink` and `logger`.
    """
    return downloader.Environment(
        clock=clock,
        connect=connection_factory(*fakes),
        metrics=LoggingMetrics() if sink is None else sink,
        logger=LOGGER if logger is None else logger,
    )


def download_response(
    fake: mock.MagicMock,
    destination: Path,
    *,
    clock: cabc.Callable[[], float] = time.monotonic,
) -> None:
    """Run one download against `fake` without touching the network."""
    connection = mock.Mock()
    connection.getresponse.return_value = fake
    downloader._download(
        "https://example.invalid/asset",
        destination,
        fake_environment(connection, clock=clock),
    )


def fixed_clock() -> float:
    """Return a clock that never advances, so only bytes can stop a copy."""
    return 0.0


def manual_clock() -> mock.Mock:
    """Return a callable clock the test moves by setting `return_value`."""
    return mock.Mock(return_value=0.0)
