"""Bounded HTTPS download for the checksum-verified PyPy archive.

The downloader refuses a declared length above `_MAX_BODY_BYTES`, stops once
the streamed body passes that ceiling, and abandons a transfer that outlives
`_TRANSFER_DEADLINE_SECONDS`. One deadline covers every redirect hop, the
header parse, and every body read: a watchdog shuts the connection's socket
down when the deadline passes, so a server that trickles bytes, or stalls a
close-delimited body, cannot hold the transfer open. Without these bounds a
stalled or oversized response would fill the host disk before the digest check
could reject it.

The clock, connection factory, and metric sink all enter through
`Environment`, so a test drives this public boundary with fakes instead of
patching the module.
"""

from __future__ import annotations

import dataclasses
import hashlib
import http.client
import time
import typing as typ
import urllib.parse

from scripts.pypy_download_deadline import (
    _DeadlineWatchdog,
    _require_time_left,
    _Transfer,
)
from scripts.pypy_download_metrics import (
    CACHED,
    CHECKSUM_MISMATCH,
    DEADLINE,
    OVERSIZE,
    REFUSED,
    SUCCESS,
    TRANSFER_ERROR,
    LoggingMetrics,
    Sample,
    report_download,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from scripts.pypy_download_deadline import Clock, Waiter
    from scripts.pypy_download_metrics import MetricsSink

# Large enough for the pinned PyPy archive with room to spare, small enough
# that a hostile or broken response cannot exhaust the host disk.
_MAX_BODY_BYTES = 512 * 1024 * 1024
# One deadline covers every redirect hop plus the body, so a server that
# trickles bytes cannot extend the transfer indefinitely.
_TRANSFER_DEADLINE_SECONDS = 900.0
# Bounded so a redirect loop fails to download rather than following hops
# forever; a release asset is served in one or two hops.
_MAX_REDIRECTS = 6
_CHUNK_BYTES = 64 * 1024


class _OversizeError(ValueError):
    """A response declared or streamed more bytes than the ceiling allows."""


class _ChecksumMismatchError(ValueError):
    """A completed body did not match the approved digest."""


class Connect(typ.Protocol):
    """Open the HTTPS connection one redirect hop needs."""

    def __call__(
        self,
        host: str,
        port: int | None,
        *,
        timeout: float,
    ) -> http.client.HTTPSConnection:
        """Open a connection to `host` on `port`, bounded by `timeout`."""


@dataclasses.dataclass(frozen=True, slots=True)
class Environment:
    """The clock, connection factory, and metric sink one download uses.

    The defaults are the real ones. A test replaces any of them at the boundary
    it drives, so no seam is monkeypatched into this module.
    """

    clock: Clock = time.monotonic
    connect: Connect = http.client.HTTPSConnection
    metrics: MetricsSink = dataclasses.field(default_factory=LoggingMetrics)


_DEFAULT_ENVIRONMENT = Environment()

# Ordered so a subclass is matched before its base: `_OversizeError` and
# `_ChecksumMismatchError` are `ValueError`s, and `TimeoutError` is an
# `OSError`. A failure matching none of them is still a transfer failure.
_FAILURE_OUTCOMES: typ.Final[tuple[tuple[type[BaseException], str], ...]] = (
    (_OversizeError, OVERSIZE),
    (_ChecksumMismatchError, CHECKSUM_MISMATCH),
    (TimeoutError, DEADLINE),
    (ValueError, REFUSED),
    (OSError, TRANSFER_ERROR),
)


def checked_download(
    url: str,
    destination: Path,
    digest: str,
    *,
    environment: Environment = _DEFAULT_ENVIRONMENT,
) -> None:
    """Fetch an HTTPS artefact and fail closed on any checksum mismatch.

    Parameters
    ----------
    url : str
        HTTPS release asset URL.
    destination : Path
        Local cache path; an existing file is reused only if its digest matches.
    digest : str
        Expected SHA-256 digest from the approved release manifest.
    environment : Environment, optional
        Clock, connection factory, and metric sink. The defaults use wall-clock
        time, the standard HTTPS connection, and the logging collector, which
        renders every sample as a structured log record — the collection path
        available to the isolated bootstrap.

    Raises
    ------
    ValueError
        If the URL is not HTTPS.
    _OversizeError
        If the response declares or streams more bytes than the ceiling allows.
    _ChecksumMismatchError
        If the body does not match the approved digest.
    TimeoutError
        If the transfer outlives the transfer deadline.
    OSError
        If an existing cache entry cannot be inspected, or the download cannot
        be transferred into the cache.
    """  # ruff: ignore[docstring-extraneous-exception] - the transfer helpers raise every listed type.
    started = environment.clock()
    pending = _pending_path(destination)
    try:
        _require_https(url)
        if _cache_is_current(destination, digest):
            outcome, written = CACHED, _size_or_zero(destination)
        else:
            _download(url, pending, environment)
            _publish(pending, destination, digest)
            outcome, written = SUCCESS, _size_or_zero(destination)
    except Exception as error:
        # Measured before the cleanup below discards what the transfer wrote.
        pending_bytes = _size_or_zero(pending)
        _report(environment, _failure_outcome(error), pending_bytes, started)
        raise
    finally:
        # Cleanup keeps its own block: even a reporting path that fails must
        # not leave a partial `.download` file behind the failed transfer.
        pending.unlink(missing_ok=True)
    _report(environment, outcome, written, started)


def _pending_path(destination: Path) -> Path:
    """Name the file a transfer fills before it is published."""
    return destination.with_suffix(".download")


def _report(
    environment: Environment, outcome: str, written: int, started: float
) -> None:
    """Publish one outcome with the bytes written and the time since `started`."""
    sample = Sample(written, environment.clock() - started)
    report_download(environment.metrics, outcome, sample)


def _require_https(url: str) -> None:
    """Refuse a download or redirect whose URL is not HTTPS."""
    if url.startswith("https://"):
        return
    msg = "binary downloads require HTTPS"
    raise ValueError(msg)


def _failure_outcome(error: BaseException) -> str:
    """Map a failed transfer onto the outcome vocabulary it belongs to."""
    for kind, outcome in _FAILURE_OUTCOMES:
        if isinstance(error, kind):
            return outcome
    return TRANSFER_ERROR


def _publish(pending: Path, destination: Path, digest: str) -> None:
    """Verify the pending bytes, then move them into the cache."""
    _require_digest(pending, destination, digest)
    pending.replace(destination)


def _size_or_zero(path: Path) -> int:
    """Report a file's size, treating an absent file as empty."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


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
        raise _ChecksumMismatchError(msg)


def _download(
    url: str,
    destination: Path,
    environment: Environment = _DEFAULT_ENVIRONMENT,
    *,
    wait: Waiter | None = None,
) -> None:
    """Follow bounded HTTPS-only release redirects into a temporary file.

    The environment supplies the clock the deadline is measured on and the
    connection factory each hop opens. Tests inject `wait` beside a manual
    clock, so a deadline is reached and a blocked read released without
    waiting for real seconds.

    Raises
    ------
    ValueError
        If a hop is not HTTPS or the redirect limit is exceeded.
    """
    transfer = _Transfer(
        environment.clock() + _TRANSFER_DEADLINE_SECONDS, environment.clock, wait
    )
    for _ in range(_MAX_REDIRECTS):
        redirect = _download_once(url, destination, transfer, environment.connect)
        if redirect is None:
            return
        url = redirect
    msg = "release redirect limit exceeded"
    raise ValueError(msg)


def _connect(url: str, connect: Connect) -> tuple[http.client.HTTPSConnection, str]:
    """Validate one hop's URL and open its HTTPS connection."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        msg = "binary downloads and redirects require HTTPS"
        raise ValueError(msg)
    target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, "")) or "/"
    connection = connect(parsed.hostname, parsed.port, timeout=60)
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
    except (OSError, http.client.HTTPException) as error:
        # Both a shut-down socket and a parser that meets a truncated status
        # line or chunk frame fail the transfer. Past the deadline the watchdog
        # caused it, so report the deadline the caller waits for; before the
        # deadline the failure is the server's and must surface unchanged.
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
        raise _OversizeError(msg)


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
            raise _OversizeError(msg)
        output.write(chunk)


def _download_once(
    url: str,
    destination: Path,
    transfer: _Transfer,
    connect: Connect,
) -> str | None:
    """Close one HTTPS connection after saving its body or resolving a redirect."""
    connection, target = _connect(url, connect)
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
