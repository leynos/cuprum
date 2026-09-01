"""Refresh the untracked shared-dictionary cache from local or HTTPS sources.

This module owns the cache freshness policy: the persisted HTTP validator
metadata, the local-source mtime comparison, and the conditional HTTPS request
with its stale-cache fallback. It builds on the cache primitives in
``typos_rollout_cache`` and validates candidate content with
``typos_rollout_dictionary``, so it stays independent of rendering.
"""

from __future__ import annotations

import email.utils
import json
import logging
import pathlib
import tomllib
import typing as typ
import urllib.error
import urllib.parse
import urllib.request

from typos_rollout_cache import (
    CacheTargets as _CacheTargets,
)
from typos_rollout_cache import (
    RefreshResult,
)
from typos_rollout_cache import (
    RemoteResponse as _RemoteResponse,
)
from typos_rollout_cache import (
    atomic_write as _atomic_write,
)
from typos_rollout_degradation import (
    _HttpsOnlyRedirectHandler,
    _record_degradation,
)
from typos_rollout_dictionary import (
    load_dictionary as _load_dictionary,
)
from typos_rollout_dictionary import (
    parse_dictionary_text as _parse_dictionary_text,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Private: an HTTP transport detail of this adapter, not part of the
# refresh policy's public surface.
_HTTP_NOT_MODIFIED = 304

# Upper bound on a remote dictionary body. The shared dictionary is a small
# TOML word list, so anything beyond this is either misconfiguration or a
# hostile endpoint; the body is rejected before it is decoded or parsed.
MAX_DICTIONARY_BYTES: typ.Final = 10 * 1024 * 1024

_logger = logging.getLogger(__name__)


def _read_metadata(path: pathlib.Path) -> dict[str, object]:
    """Read best-effort HTTP freshness metadata."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # Metadata is a best-effort cache hint: unreadable, non-UTF-8, or
        # malformed sidecars degrade to "no validators known" rather than
        # failing the refresh.
        return {}
    return value if isinstance(value, dict) else {}


def _write_metadata(
    path: pathlib.Path,
    metadata: cabc.Mapping[str, object],
) -> None:
    """Atomically write HTTP freshness metadata."""
    _atomic_write(path, (json.dumps(metadata, sort_keys=True) + "\n").encode())


def _valid_cache(cache: pathlib.Path) -> bool:
    """Return whether *cache* contains a valid shared dictionary."""
    try:
        _load_dictionary(cache)
    except (
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ):
        return False
    return True


def _remote_is_not_newer(
    saved: cabc.Mapping[str, object],
    headers: cabc.Mapping[str, str],
) -> bool:
    """Return whether HTTP validators prove the response is not newer."""
    etag = headers.get("ETag")
    if etag is not None:
        # An ETag is the strongest validator on offer: when one is present it
        # alone decides freshness. A mismatch must not fall through to
        # Last-Modified, which can stay unchanged while the entity changes.
        return etag == saved.get("etag")
    modified = headers.get("Last-Modified")
    saved_modified = saved.get("last_modified")
    if not isinstance(modified, str) or not isinstance(saved_modified, str):
        return False
    try:
        return email.utils.parsedate_to_datetime(
            modified
        ) <= email.utils.parsedate_to_datetime(saved_modified)
    except (TypeError, ValueError):
        return modified == saved_modified


def _local_cache_is_current(
    cache: pathlib.Path,
    saved: cabc.Mapping[str, object],
    source_name: str,
    source_mtime_ns: int,
) -> bool:
    """Return whether metadata proves a valid local-source cache is current."""
    saved_mtime = saved.get("mtime_ns")
    has_matching_source = saved.get("source") == source_name
    has_new_enough_mtime = (
        isinstance(saved_mtime, int) and source_mtime_ns <= saved_mtime
    )
    return _valid_cache(cache) and has_matching_source and has_new_enough_mtime


def _refresh_local(
    source: pathlib.Path,
    cache: pathlib.Path,
    metadata: pathlib.Path,
) -> RefreshResult:
    """Refresh from a local authoritative copy when it is newer."""
    source_stat = source.stat()
    source_name = str(source.resolve())
    saved = _read_metadata(metadata)
    if _local_cache_is_current(
        cache,
        saved,
        source_name,
        source_stat.st_mtime_ns,
    ):
        return RefreshResult("current", cache)
    content = source.read_bytes()
    _parse_dictionary_text(content.decode())
    _atomic_write(cache, content)
    _write_metadata(
        metadata,
        {"source": source_name, "mtime_ns": source_stat.st_mtime_ns},
    )
    return RefreshResult("refreshed", cache)


def _conditional_headers(saved: cabc.Mapping[str, object]) -> dict[str, str]:
    """Build conditional HTTP headers from persisted validators."""
    headers: dict[str, str] = {}
    etag = saved.get("etag")
    if isinstance(etag, str):
        headers["If-None-Match"] = etag
    last_modified = saved.get("last_modified")
    if isinstance(last_modified, str):
        headers["If-Modified-Since"] = last_modified
    return headers


def _https_request(
    source: str,
    headers: cabc.Mapping[str, str],
) -> urllib.request.Request:
    """Build a request after constraining the shared source to HTTPS."""
    scheme = urllib.parse.urlsplit(source).scheme
    if scheme != "https":
        # The URL is omitted deliberately: it can carry userinfo or query
        # tokens, and an exception message reaches tracebacks and any caller
        # doing ``str(exc)``, neither of which is redacted like the logs are.
        message = f"shared dictionary URL must use HTTPS, got scheme {scheme!r}"
        raise ValueError(message)
    return urllib.request.Request(source, headers=dict(headers))  # ruff: ignore[suspicious-url-open-usage] - HTTPS is required above.


def _write_remote_cache(
    targets: _CacheTargets,
    content: bytes,
    headers: cabc.Mapping[str, str],
) -> RefreshResult:
    """Validate and atomically persist an HTTP dictionary response."""
    if len(content) > MAX_DICTIONARY_BYTES:
        message = (
            f"shared dictionary response exceeds the {MAX_DICTIONARY_BYTES}-byte limit"
        )
        raise ValueError(message)
    _parse_dictionary_text(content.decode())
    _atomic_write(targets.cache, content)
    _write_metadata(
        targets.metadata,
        {
            "etag": headers.get("ETag"),
            "last_modified": headers.get("Last-Modified"),
        },
    )
    return RefreshResult("refreshed", targets.cache)


def _remote_response_result(
    targets: _CacheTargets,
    saved: cabc.Mapping[str, object],
    response: _RemoteResponse,
) -> RefreshResult:
    """Return the cache result for a successful HTTP response."""
    if response.status == _HTTP_NOT_MODIFIED and _valid_cache(targets.cache):
        return RefreshResult("current", targets.cache)
    if _valid_cache(targets.cache) and _remote_is_not_newer(saved, response.headers):
        return RefreshResult("current", targets.cache)
    # Read one byte past the limit so _write_remote_cache can tell an
    # exactly-at-limit body from an oversized one without unbounded buffering.
    content = response.read(MAX_DICTIONARY_BYTES + 1)
    return _write_remote_cache(targets, content, response.headers)


def _stale_cache_or_raise(
    cache: pathlib.Path,
    error: OSError | urllib.error.URLError | ValueError,
) -> RefreshResult:
    """Return a valid stale cache or propagate the download failure."""
    if _valid_cache(cache):
        _record_degradation("stale_cache")
        _logger.warning(
            "Reusing stale shared dictionary cache after a refresh failure",
            extra={
                "event": "typos_rollout.stale_cache",
                "error_type": type(error).__name__,
            },
        )
        return RefreshResult("stale-cache", cache)
    raise error


def _http_error_result(
    cache: pathlib.Path,
    error: urllib.error.HTTPError,
) -> RefreshResult:
    """Translate an HTTP failure into the available cache result."""
    if error.code == _HTTP_NOT_MODIFIED and _valid_cache(cache):
        return RefreshResult("current", cache)
    return _stale_cache_or_raise(cache, error)


def _refresh_http(
    source: str,
    cache: pathlib.Path,
    metadata: pathlib.Path,
) -> RefreshResult:
    """Refresh a cache from a validated HTTPS source with stale fallback."""
    saved = _read_metadata(metadata)
    request = _https_request(source, _conditional_headers(saved))
    # A dedicated opener is required so the redirect policy applies; the module
    # level ``urlopen`` would use the default opener and follow a downgrade.
    opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
    try:
        with opener.open(
            request,
            timeout=30.0,
        ) as response:
            return _remote_response_result(
                _CacheTargets(cache, metadata), saved, response
            )
    except urllib.error.HTTPError as error:
        return _http_error_result(cache, error)
    # ValueError covers oversized and unparsable response bodies (including
    # ``tomllib.TOMLDecodeError``): a bad download degrades to a valid stale
    # cache rather than failing the refresh outright.
    except (OSError, urllib.error.URLError, ValueError) as error:
        return _stale_cache_or_raise(cache, error)


def refresh_base(
    source: str | pathlib.Path,
    cache: pathlib.Path,
    *,
    metadata: pathlib.Path,
    offline: bool = False,
) -> RefreshResult:
    """Refresh an untracked base cache when the authoritative copy is newer.

    Parameters
    ----------
    source : str | pathlib.Path
        The authoritative dictionary, either a local path or an
        ``https://`` URL.
    cache : pathlib.Path
        The untracked local cache file that is refreshed.
    metadata : pathlib.Path
        The sidecar JSON file holding freshness validators for *cache*.
    offline : bool
        When True, skip the network entirely and reuse a valid cache.

    Returns
    -------
    RefreshResult
        The outcome of the refresh attempt.

    Raises
    ------
    FileNotFoundError
        If *offline* is set and no valid cached dictionary exists.
    OSError
        If the local or HTTP refresh path fails and no stale cache is
        available.
    ValueError
        If the refreshed dictionary source cannot be parsed, or a remote
        response body exceeds ``MAX_DICTIONARY_BYTES`` and no valid stale
        cache is available to fall back on.
    TypeError
        If the refreshed dictionary source is not a mapping.
    UnicodeDecodeError
        If a local authoritative dictionary is not valid UTF-8.
    tomllib.TOMLDecodeError
        If the refreshed dictionary source is not valid TOML.
    urllib.error.URLError
        If the HTTP refresh path fails and no stale cache is available.
    """  # ruff: ignore[docstring-extraneous-exception] - refresh and parse errors propagate from the callees
    if offline:
        if not _valid_cache(cache):
            message = f"no cached shared dictionary at {cache}"
            raise FileNotFoundError(message)
        _record_degradation("offline_cache")
        _logger.info(
            "Reusing cached shared dictionary; offline mode skipped the refresh",
            extra={"event": "typos_rollout.offline_cache"},
        )
        return RefreshResult("offline-cache", cache)
    if isinstance(source, pathlib.Path) or "://" not in str(source):
        return _refresh_local(pathlib.Path(source), cache, metadata)
    return _refresh_http(str(source), cache, metadata)
