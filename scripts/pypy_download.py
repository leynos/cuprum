"""Bounded HTTPS download for the checksum-verified PyPy archive.

The downloader refuses a declared length above `_MAX_BODY_BYTES`, stops once
the streamed body passes that ceiling, and abandons a transfer that outlives
`_TRANSFER_DEADLINE_SECONDS`. One deadline covers every redirect hop, the
header parse, and every body read: a watchdog shuts the connection's socket
down when the deadline passes, so a server that trickles bytes cannot hold the
transfer open. Without these bounds a stalled or oversized response would fill
the host disk before the digest check could reject it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import http.client
import socket
import threading
import time
import typing as typ
import urllib.parse

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    type Clock = cabc.Callable[[], float]

# Large enough for the pinned PyPy archive with room to spare, small enough
# that a hostile or broken response cannot exhaust the host disk.
_MAX_BODY_BYTES = 512 * 1024 * 1024
# One deadline covers every redirect hop plus the body, so a server that
# trickles bytes cannot extend the transfer indefinitely.
_TRANSFER_DEADLINE_SECONDS = 900.0
_CHUNK_BYTES = 64 * 1024


def checked_download(url: str, destination: Path, digest: str) -> None:
    """Fetch an HTTPS artefact and fail closed on any checksum mismatch.

    Parameters
    ----------
    url : str
        HTTPS release asset URL.
    destination : Path
        Local cache path; an existing file is reused only if its digest matches.
    digest : str
        Expected SHA-256 digest from the approved release manifest.

    Raises
    ------
    ValueError
        If the URL is not HTTPS or downloaded bytes do not match the pin.
    OSError
        If an existing cache entry cannot be inspected, or the download cannot
        be transferred into the cache.
    """  # ruff: ignore[docstring-extraneous-exception] - OSError propagates from _cache_is_current and _digest.
    if not url.startswith("https://"):
        msg = "binary downloads require HTTPS"
        raise ValueError(msg)
    if _cache_is_current(destination, digest):
        return
    pending = destination.with_suffix(".download")
    try:
        _download(url, pending)
        _require_digest(pending, destination, digest)
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)


def _cache_is_current(destination: Path, digest: str) -> bool:
    """Report whether an existing cache entry already matches the pin.

    An entry that cannot be read is not a miss: the failure is raised, so
    `checked_download` never answers an unreadable entry with a fresh download
    that would overwrite it.

    Returns
    -------
    bool
        ``True`` only for a readable entry whose digest already matches.
    """
    try:
        current = _digest(destination)
    except FileNotFoundError:
        return False
    return current == digest


def _require_digest(pending: Path, destination: Path, digest: str) -> None:
    """Refuse downloaded bytes that do not match the approved digest."""
    if _digest(pending) != digest:
        msg = f"binary checksum mismatch: {destination.name}"
        raise ValueError(msg)


@dataclasses.dataclass(frozen=True, slots=True)
class _Transfer:
    """The one deadline and clock every redirect hop of a download shares."""

    deadline: float
    clock: Clock

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

    A deadline checked only between reads cannot stop a server that trickles
    header bytes, because each individual read still finishes inside the
    socket timeout. Shutting the socket down makes the blocked read fail, so
    the deadline covers the header parse as well as the body.
    """

    def __init__(
        self,
        connection: http.client.HTTPSConnection,
        transfer: _Transfer,
    ) -> None:
        """Arm a watchdog for `connection` under `transfer`'s deadline."""
        self._connection = connection
        self._transfer = transfer
        self._stop = threading.Event()
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
        """Wait for the deadline, then shut the socket down."""
        if self._stop.wait(self._transfer.remaining()):
            return
        sock = self._connection.sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


def _download(
    url: str,
    destination: Path,
    *,
    clock: Clock = time.monotonic,
) -> None:
    """Follow bounded HTTPS-only release redirects into a temporary file."""
    # Tests pass a controlled clock so a deadline is reached without waiting.
    transfer = _Transfer(clock() + _TRANSFER_DEADLINE_SECONDS, clock)
    for _ in range(6):
        redirect = _download_once(url, destination, transfer)
        if redirect is None:
            return
        url = redirect
    msg = "release redirect limit exceeded"
    raise ValueError(msg)


def _connect(url: str) -> tuple[http.client.HTTPSConnection, str]:
    """Validate one hop's URL and open its HTTPS connection."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        msg = "binary downloads and redirects require HTTPS"
        raise ValueError(msg)
    target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, "")) or "/"
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=60)
    return connection, target


def _redirect_url(url: str, response: http.client.HTTPResponse) -> str | None:
    """Resolve a redirect target or validate a terminal download response."""
    if response.status in {301, 302, 303, 307, 308}:
        location = response.getheader("Location")
        if location is None:
            msg = "release redirect has no Location"
            raise ValueError(msg)
        return urllib.parse.urljoin(url, location)
    if response.status != http.client.OK:
        msg = f"release download failed: HTTP {response.status}"
        raise OSError(msg)
    return None


def _fetch_body(
    connection: http.client.HTTPSConnection,
    url: str,
    destination: Path,
    transfer: _Transfer,
) -> str | None:
    """Save the response body, or resolve one redirect hop instead."""
    # The header parse blocks inside `getresponse`, so the watchdog must start
    # before it rather than beside the body copy.
    _require_time_left(transfer)
    try:
        with (
            _DeadlineWatchdog(connection, transfer),
            connection.getresponse() as response,
        ):
            redirect = _redirect_url(url, response)
            if redirect is not None:
                return redirect
            _reject_oversize_declaration(response)
            with destination.open("wb") as output:
                _copy_body(response, output, transfer)
            return None
    except TimeoutError:
        raise
    except OSError as error:
        if transfer.expired():
            msg = "release download exceeded the transfer deadline"
            raise TimeoutError(msg) from error
        raise


def _reject_oversize_declaration(response: http.client.HTTPResponse) -> None:
    """Refuse a declared body length above the asset ceiling before reading."""
    declared = response.getheader("Content-Length")
    if declared is None:
        return
    try:
        length = int(declared)
    except ValueError:
        msg = f"release download declared a non-numeric Content-Length: {declared}"
        raise ValueError(msg) from None
    if length > _MAX_BODY_BYTES:
        msg = (
            f"release download declared {length} bytes, above the "
            f"{_MAX_BODY_BYTES}-byte limit"
        )
        raise ValueError(msg)


def _copy_body(
    response: http.client.HTTPResponse,
    output: typ.BinaryIO,
    transfer: _Transfer,
) -> None:
    """Copy chunks until EOF, refusing an overflow or an expired deadline."""
    written = 0
    while True:
        _require_time_left(transfer)
        # `read` blocks until it fills the buffer or reaches EOF, so a server
        # trickling one byte per socket timeout could hold a single call far
        # past the deadline. `read1` returns after at most one socket read, so
        # the check above runs again within the socket timeout.
        chunk = response.read1(_CHUNK_BYTES)
        if not chunk:
            # A socket shut down at the deadline reads as EOF, so the deadline
            # is checked again before the short body is accepted as complete.
            _require_time_left(transfer)
            return
        written += len(chunk)
        if written > _MAX_BODY_BYTES:
            msg = f"release download exceeded the {_MAX_BODY_BYTES}-byte limit"
            raise ValueError(msg)
        output.write(chunk)


def _download_once(
    url: str,
    destination: Path,
    transfer: _Transfer,
) -> str | None:
    """Close one HTTPS connection after saving its body or resolving a redirect."""
    connection, target = _connect(url)
    try:
        connection.request(
            "GET",
            target,
            headers={"User-Agent": "cuprum-boundary-verification"},
        )
        return _fetch_body(connection, url, destination, transfer)
    finally:
        connection.close()


def _digest(path: Path) -> str:
    """Hash a release artefact without loading the bundle into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()
