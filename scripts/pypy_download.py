"""Bounded HTTPS download for the checksum-verified PyPy archive.

The downloader refuses a declared length above `_MAX_BODY_BYTES`, stops once
the streamed body passes that ceiling, and abandons a transfer that outlives
`_TRANSFER_DEADLINE_SECONDS`. One deadline covers every redirect hop, the
request that opens it, the header parse, and every body read: a watchdog shuts
the connection's socket down when the deadline passes, so a server that
trickles bytes, or stalls a close-delimited body, cannot hold the transfer
open. Without these bounds a stalled or oversized response would fill the host
disk before the digest check could reject it.

Each transfer stages its bytes in a file no other transfer shares and publishes
that file only after the digest matches, so two installers racing on one cache
path cannot truncate or remove each other's bytes.

The clock, connection factory, and metric sink all enter through
`Environment`, so a test drives this public boundary with fakes instead of
patching the module.
"""

from __future__ import annotations

import dataclasses
import http.client
import time
import typing as typ
import urllib.parse

from scripts.pypy_download_cache import (
    ChecksumMismatchError,
    digest_of,
    publish,
    stage,
)
from scripts.pypy_download_deadline import (
    _arm_socket,
    _deadline_failures,
    _DeadlineWatchdog,
    _hop_timeout,
    _require_time_left,
    _Transfer,
)
from scripts.pypy_download_metrics import (
    CACHED,
    CHECKSUM_MISMATCH,
    DEADLINE,
    LOGGER,
    OVERSIZE,
    REFUSED,
    SUCCESS,
    TRANSFER_ERROR,
    LoggingMetrics,
    Sample,
    report_download,
)

if typ.TYPE_CHECKING:
    import logging
    from pathlib import Path

    from scripts.pypy_download_deadline import Clock, Waiter
    from scripts.pypy_download_metrics import MetricsSink

# Large enough for the pinned PyPy archive with room to spare, small enough
# that a hostile or broken response cannot exhaust the host disk.
_MAX_BODY_BYTES = 512 * 1024 * 1024
# One deadline covers every hop, its request, and the body it streams.
_TRANSFER_DEADLINE_SECONDS = 900.0
# Bounded so a redirect loop fails to download rather than following hops
# forever; a release asset is served in one or two hops.
_MAX_REDIRECTS = 6
_CHUNK_BYTES = 64 * 1024
# A killed transfer can leave a staging file behind, so a later run removes
# siblings older than twice the deadline: no live transfer, whose own file is
# bounded by that deadline, can be that old.
_STALE_STAGING_SECONDS = 2 * _TRANSFER_DEADLINE_SECONDS


class _OversizeError(ValueError):
    """A response declared or streamed more bytes than the ceiling allows."""


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
    """The clock, connection factory, metric sink, and logger one download uses.

    The defaults are the real ones; a test replaces them at the boundary it
    drives, and the logger is a field so one download's span record can be
    redirected without reconfiguring the shared module logger.
    """

    clock: Clock = time.monotonic
    connect: Connect = http.client.HTTPSConnection
    metrics: MetricsSink = dataclasses.field(default_factory=LoggingMetrics)
    logger: logging.Logger = LOGGER


_DEFAULT_ENVIRONMENT = Environment()

# Ordered so a subclass is matched before its base: `_OversizeError` and
# `ChecksumMismatchError` are `ValueError`s, and `TimeoutError` is an
# `OSError`. A failure matching none of them is still a transfer failure.
_FAILURE_OUTCOMES: typ.Final[tuple[tuple[type[BaseException], str], ...]] = (
    (_OversizeError, OVERSIZE),
    (ChecksumMismatchError, CHECKSUM_MISMATCH),
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

    Two calls for one destination may run at the same time: each stages into
    its own file, verifies that file, and replaces the cache entry atomically,
    so neither call can publish bytes it did not verify.

    Parameters
    ----------
    url : str
        HTTPS release asset URL.
    destination : Path
        Local cache path; an existing file is reused only if its digest matches.
    digest : str
        Expected SHA-256 digest from the approved release manifest.
    environment : Environment, optional
        Clock, connection factory, metric sink, and span logger. The defaults
        use wall-clock time, the standard HTTPS connection, the logging
        collector that renders every sample as a structured record, and this
        module's logger — the collection path the isolated bootstrap has.

    Raises
    ------
    ValueError
        If the URL is not HTTPS.
    _OversizeError
        If the response declares or streams more bytes than the ceiling allows.
    ChecksumMismatchError
        If the body does not match the approved digest.
    TimeoutError
        If the transfer outlives the transfer deadline.
    OSError
        If an existing cache entry cannot be inspected, or the download cannot
        be transferred into the cache.
    """  # ruff: ignore[docstring-extraneous-exception] - the transfer helpers raise every listed type.
    started = environment.clock()
    pending: Path | None = None
    try:
        _require_https(url)
        if _cache_is_current(destination, digest):
            outcome, written = CACHED, _size_or_zero(destination)
        else:
            pending = stage(destination, stale_after_seconds=_STALE_STAGING_SECONDS)
            _download(url, pending, environment)
            publish(pending, destination, digest)
            outcome, written = SUCCESS, _size_or_zero(destination)
    except Exception as error:
        # Measured before the cleanup below discards what the transfer wrote.
        pending_bytes = _size_or_zero(pending) if pending is not None else 0
        _report(environment, _failure_outcome(error), pending_bytes, started)
        raise
    finally:
        # Cleanup keeps its own block: even a reporting path that fails must
        # not leave this transfer's staging file behind the failed transfer.
        if pending is not None:
            pending.unlink(missing_ok=True)
    _report(environment, outcome, written, started)


def _report(
    environment: Environment, outcome: str, written: int, started: float
) -> None:
    """Publish one outcome with the bytes written and the time since `started`."""
    sample = Sample(written, environment.clock() - started)
    report_download(environment.metrics, outcome, sample, logger=environment.logger)


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
        current = digest_of(destination)
    except FileNotFoundError:
        return False
    return current == digest


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
    waiting for real seconds. The waiter is also handed the watchdog's stop
    event, so one that blocks on its own condition is released when the
    transfer ends rather than holding the download's exit in `join`.

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


def _connect(
    url: str, connect: Connect, timeout: float
) -> tuple[http.client.HTTPSConnection, str]:
    """Validate one hop's URL and open its connection within `timeout`."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        msg = "binary downloads and redirects require HTTPS"
        raise ValueError(msg)
    target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, "")) or "/"
    connection = connect(parsed.hostname, parsed.port, timeout=timeout)
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
    _require_time_left(transfer)
    with connection.getresponse() as response:
        redirect = _redirect_url(url, response)
        if redirect is not None:
            return redirect
        _reject_oversize_declaration(response)
        with destination.open("wb") as output:
            _copy_body(response, output, transfer)
        return None


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
    """Close one hop's connection after saving its body or resolving a redirect."""
    connection, target = _connect(url, connect, _hop_timeout(transfer))
    try:
        with _deadline_failures(transfer):
            connection.connect()
            _require_time_left(transfer)
            with _DeadlineWatchdog(connection, transfer):
                _send_request(connection, target, transfer)
                return _fetch_body(connection, url, destination, transfer)
    finally:
        connection.close()


def _send_request(
    connection: http.client.HTTPSConnection, target: str, transfer: _Transfer
) -> None:
    """Send one GET bounded by the time the transfer has left."""
    _arm_socket(connection, transfer)
    connection.request(
        "GET", target, headers={"User-Agent": "cuprum-boundary-verification"}
    )
