"""Shared test doubles for the bounded PyPy download contracts.

Each helper builds a fake response, clock, or connection sequence so the
transfer tests run without a socket, a network call, or a real-time wait.
"""

from __future__ import annotations

import http.client
import typing as typ
from unittest import mock

from scripts import pypy_download as downloader

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    import pytest


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


def download_response(
    fake: mock.MagicMock,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: cabc.Callable[[], float] | None = None,
) -> None:
    """Run one download against `fake` without touching the network."""
    connection = mock.Mock()
    connection.getresponse.return_value = fake
    monkeypatch.setattr(
        downloader.http.client,
        "HTTPSConnection",
        lambda *_args, **_kwargs: connection,
    )
    if clock is None:
        downloader._download("https://example.invalid/asset", destination)
    else:
        downloader._download("https://example.invalid/asset", destination, clock=clock)


def patch_connections(
    monkeypatch: pytest.MonkeyPatch, connections: cabc.Iterable[mock.Mock]
) -> None:
    """Give each `HTTPSConnection` call the next fake connection."""
    remaining = iter(connections)
    monkeypatch.setattr(
        downloader.http.client,
        "HTTPSConnection",
        lambda *_args, **_kwargs: next(remaining),
    )


def download_with_clock(monkeypatch: pytest.MonkeyPatch, clock: mock.Mock) -> None:
    """Run `checked_download`'s internal download against `clock`."""
    download = downloader._download

    def timed_download(url: str, destination: Path) -> None:
        """Run the real download with the shared test clock."""
        download(url, destination, clock=clock)

    monkeypatch.setattr(downloader, "_download", timed_download)


def fixed_clock() -> float:
    """Return a clock that never advances, so only bytes can stop a copy."""
    return 0.0


def manual_clock() -> mock.Mock:
    """Return a callable clock the test moves by setting `return_value`."""
    return mock.Mock(return_value=0.0)
