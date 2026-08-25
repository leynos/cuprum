"""GitHub HTTP transport for benchmark baseline discovery and downloads."""

from __future__ import annotations

import http.client
import inspect
import json
import time
import typing as typ
import urllib.error
import urllib.parse
import urllib.request

from benchmarks._validation import _require_int, _require_mapping

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_REQUEST_TIMEOUT_SECONDS = 10.0
_ARCHIVE_READ_CHUNK_BYTES = 64 * 1024
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_JSON_RESPONSE_BYTES = 1 * 1024 * 1024
_RETRY_DELAYS_SECONDS = (0.5, 1.0)
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX = 600
_REDIRECT_ARGUMENT_SIGNATURE = inspect.Signature(
    tuple(
        inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for name in ("fp", "code", "msg", "headers", "newurl")
    )
)
_GITHUB_REDIRECT_HEADERS_TO_STRIP = (
    "Authorization",
    "X-github-api-version",
)

type _RedirectRequestArguments = tuple[
    typ.IO[bytes],
    int,
    str,
    http.client.HTTPMessage,
    str,
]


def _require_type[T](value: object, expected_type: type[T], *, name: str) -> T:
    """Validate that *value* has the expected runtime type."""
    if not isinstance(value, expected_type):
        msg = f"{name} must be a {expected_type.__name__}"
        raise TypeError(msg)
    return value


def _redirect_request_arguments(
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> _RedirectRequestArguments:
    """Normalize the positional and keyword forms supported by urllib."""
    bound_arguments = _REDIRECT_ARGUMENT_SIGNATURE.bind(*args, **kwargs).arguments
    fp = bound_arguments["fp"]
    code = _require_int(bound_arguments["code"], name="code")
    message = _require_type(bound_arguments["msg"], str, name="msg")
    headers = _require_type(
        bound_arguments["headers"],
        http.client.HTTPMessage,
        name="headers",
    )
    newurl = _require_type(bound_arguments["newurl"], str, name="newurl")

    # urllib owns this callback contract, so its opaque response stream is the
    # authoritative source of the IO type at this adapter boundary.
    return typ.cast("typ.IO[bytes]", fp), code, message, headers, newurl


def _should_retry_request_failure(exc: Exception) -> bool:
    """Return ``True`` when a GitHub API failure is transient."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == _HTTP_TOO_MANY_REQUESTS or (
            _HTTP_SERVER_ERROR_MIN <= exc.code < _HTTP_SERVER_ERROR_MAX
        )
    return isinstance(exc, urllib.error.URLError)


def _retry_delay_or_raise(
    exc: urllib.error.URLError,
    delay: float | None,
) -> float:
    """Return the retry delay or re-raise the request failure."""
    if not _should_retry_request_failure(exc):
        raise exc
    if delay is None:
        raise exc
    return delay


def _with_retry[T](
    operation: cabc.Callable[[], T],
    *,
    description: str,
) -> T:
    """Run *operation* with bounded retry/backoff for transient HTTP failures."""
    del description  # Retain the keyword contract; failures preserve their exception.
    retry_schedule = iter((*_RETRY_DELAYS_SECONDS, None))
    while True:
        delay = next(retry_schedule)
        try:
            return operation()
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            try:
                retry_delay = _retry_delay_or_raise(exc, delay)
            finally:
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
            time.sleep(retry_delay)


class _ArtefactArchiveRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip GitHub-only headers when following cross-origin archive redirects."""

    @staticmethod
    def _strip_cross_origin_headers(
        req: urllib.request.Request,
        redirected_request: urllib.request.Request,
    ) -> None:
        """Strip sensitive headers when a redirect crosses host boundaries."""
        source_parts = urllib.parse.urlsplit(req.full_url)
        destination_parts = urllib.parse.urlsplit(redirected_request.full_url)
        source_origin = (source_parts.scheme, source_parts.netloc)
        destination_origin = (destination_parts.scheme, destination_parts.netloc)
        if source_origin == destination_origin:
            return
        for header in _GITHUB_REDIRECT_HEADERS_TO_STRIP:
            redirected_request.remove_header(header)

    @typ.override
    def redirect_request(
        self,
        req: urllib.request.Request,
        *args: object,
        **kwargs: object,
    ) -> urllib.request.Request | None:
        fp, code, msg, headers, newurl = _redirect_request_arguments(args, kwargs)
        redirected_request = super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )
        if redirected_request is None:
            return None
        self._strip_cross_origin_headers(req, redirected_request)
        return redirected_request


def _load_json_response(*, url: str, token: str) -> cabc.Mapping[str, object]:
    """Load a GitHub API JSON response."""
    _require_https_url(url)
    request = urllib.request.Request(  # noqa: S310 - URL is selected by trusted caller
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "cuprum-benchmark-ratchet",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    opener = urllib.request.build_opener(_ArtefactArchiveRedirectHandler())

    def _open_json_response() -> cabc.Mapping[str, object]:
        with opener.open(
            request,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            chunks: list[bytes] = []
            response_size = 0
            while chunk := response.read(_ARCHIVE_READ_CHUNK_BYTES):
                response_size += len(chunk)
                if response_size > _MAX_JSON_RESPONSE_BYTES:
                    msg = (
                        f"JSON response from {url} exceeds "
                        f"{_MAX_JSON_RESPONSE_BYTES} bytes"
                    )
                    raise ValueError(msg)
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
        return _require_mapping(payload, name=f"response from {url}")

    return _with_retry(_open_json_response, description=f"load JSON from {url}")


def _download_bytes(*, url: str, token: str) -> bytes:
    """Download raw bytes from an authenticated URL."""
    _require_https_url(url)
    request = urllib.request.Request(  # noqa: S310 - URL is returned by the GitHub API
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "cuprum-benchmark-ratchet",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = urllib.request.build_opener(_ArtefactArchiveRedirectHandler())

    def _open_archive() -> bytes:
        with opener.open(
            request,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            chunks: list[bytes] = []
            archive_size = 0
            while chunk := response.read(_ARCHIVE_READ_CHUNK_BYTES):
                archive_size += len(chunk)
                if archive_size > _MAX_ARCHIVE_BYTES:
                    msg = f"archive from {url} exceeds {_MAX_ARCHIVE_BYTES} bytes"
                    raise ValueError(msg)
                chunks.append(chunk)
            return b"".join(chunks)

    return _with_retry(_open_archive, description=f"download archive from {url}")


def _require_https_url(url: str) -> None:
    """Reject authenticated request targets that do not use HTTPS."""
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        msg = f"authenticated request URL must use HTTPS: {url}"
        raise ValueError(msg)
