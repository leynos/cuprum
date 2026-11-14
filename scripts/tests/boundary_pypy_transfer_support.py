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
from scripts.pypy_download_metrics import LoggingMetrics

if typ.TYPE_CHECKING:
    import collections.abc as cabc
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


def fake_environment(
    *fakes: mock.Mock,
    clock: cabc.Callable[[], float] = time.monotonic,
    sink: MetricsSink | None = None,
) -> downloader.Environment:
    """Build an environment whose connections come from `fakes` in order.

    The sink defaults to the same logging collector the bootstrap uses, so a
    case that inspects log records needs to supply nothing.

    Returns
    -------
    downloader.Environment
        An environment that serves each hop from the next fake in `fakes` and
        reports through `sink`.
    """
    return downloader.Environment(
        clock=clock,
        connect=connection_factory(*fakes),
        metrics=LoggingMetrics() if sink is None else sink,
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
