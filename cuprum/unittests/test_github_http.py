"""Unit tests for the benchmark GitHub HTTP transport."""

from __future__ import annotations

import http.client
import typing as typ
import urllib.error
import urllib.request
from unittest import mock

import pytest

from benchmarks import _github_http
from benchmarks._github_http import _ArtefactArchiveRedirectHandler, _with_retry
from benchmarks.fetch_main_benchmark_baseline import (
    _download_bytes,
    _load_json_response,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_BOUNDED_URL = "https://api.github.com/repos/leynos/cuprum/actions/runs"
_BOUNDED_TOKEN = "".join(("tok", "en"))


def test_load_json_response_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON responses beyond the configured bound should be rejected."""

    class _Response:
        """Minimal response that exceeds the configured JSON limit."""

        def __init__(self) -> None:
            """Track the chunks that make the response oversized."""
            self._chunks = iter((b"{}", b"too"))

        def __enter__(self) -> _Response:
            """Return the stub response for use as a context manager."""
            return self

        def __exit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            """Exit the context manager without suppressing exceptions."""

        def read(self, size: int) -> bytes:
            """Return bounded chunks until the simulated response is exhausted."""
            assert size > 0, "JSON reads should request a bounded chunk"
            return next(self._chunks, b"")

    class _Opener:
        """Minimal urllib opener returning the oversized response."""

        @staticmethod
        def open(
            request: urllib.request.Request,
            *,
            timeout: float,
        ) -> _Response:
            """Return the response while accepting the request contract."""
            del request, timeout
            return _Response()

    received_handler: urllib.request.BaseHandler | None = None

    def build_opener(handler: urllib.request.BaseHandler) -> _Opener:
        """Capture the installed redirect handler and return the stub opener."""
        nonlocal received_handler
        received_handler = handler
        return _Opener()

    monkeypatch.setattr(
        "benchmarks._github_http.urllib.request.build_opener", build_opener
    )
    monkeypatch.setattr("benchmarks._github_http._MAX_JSON_RESPONSE_BYTES", 4)

    with pytest.raises(ValueError, match=r"JSON response .* exceeds 4 bytes"):
        _load_json_response(
            url="https://example.invalid/workflow-runs",
            token="".join(("tok", "en")),
        )

    assert isinstance(received_handler, _ArtefactArchiveRedirectHandler), (
        "JSON loading must install the archive-safe redirect handler"
    )


@pytest.mark.parametrize("status", [429, 500, 599])
def test_with_retry_retries_transient_http_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """Rate limiting and server failures should use the retry schedule."""
    errors = [
        urllib.error.HTTPError(
            "https://example.invalid",
            status,
            "transient",
            http.client.HTTPMessage(),
            None,
        )
        for _ in range(2)
    ]
    operation = mock.Mock(side_effect=[*errors, "done"])
    delays: list[float] = []
    monkeypatch.setattr("benchmarks._github_http.time.sleep", delays.append)

    result = _with_retry(operation, description="test")

    assert result == "done", "transient HTTP failures should eventually return"
    assert operation.call_count == 3, "transient HTTP failures should be retried twice"
    assert delays == [0.5, 1.0], "transient HTTP failures should use both delays"
    assert all(error.closed for error in errors), "handled HTTP errors should close"


@pytest.mark.parametrize("status", [404, 499, 600])
def test_with_retry_raises_non_transient_http_error(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """A non-transient HTTP status should fail without sleeping."""
    error = urllib.error.HTTPError(
        "https://example.invalid",
        status,
        "missing",
        http.client.HTTPMessage(),
        None,
    )
    operation = mock.Mock(side_effect=error)
    delays: list[float] = []
    monkeypatch.setattr("benchmarks._github_http.time.sleep", delays.append)

    with pytest.raises(urllib.error.HTTPError) as raised:
        _with_retry(operation, description="test")

    assert raised.value is error, "retry should propagate the non-transient error"
    assert operation.call_count == 1, "non-transient errors should stop after one call"
    assert not delays, "non-transient errors should not schedule a retry delay"
    assert error.closed, "re-raised HTTP errors should close before propagation"


@pytest.mark.parametrize(
    ("wrapper", "expected_description"),
    [
        (_load_json_response, f"load JSON from {_BOUNDED_URL}"),
        (_download_bytes, f"download archive from {_BOUNDED_URL}"),
    ],
    ids=["json", "archive"],
)
def test_bounded_wrappers_describe_their_retry_operation(
    monkeypatch: pytest.MonkeyPatch,
    wrapper: cabc.Callable[..., object],
    expected_description: str,
) -> None:
    """Each wrapper labels its own retry operation.

    ``_with_retry`` discards the description, so nothing at runtime would
    reveal the two wrappers swapping labels now that both route through the
    shared bounded reader.
    """
    captured: list[str] = []

    def _capture_description(
        operation: cabc.Callable[[], bytes],
        *,
        description: str,
    ) -> bytes:
        """Record the retry description without performing the read."""
        del operation
        captured.append(description)
        return b'{"ok": true}'

    monkeypatch.setattr(_github_http, "_with_retry", _capture_description)

    wrapper(url=_BOUNDED_URL, token=_BOUNDED_TOKEN)

    assert captured == [expected_description], (
        f"expected the retry description {expected_description!r}, got {captured!r}"
    )


def test_load_json_response_requires_a_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-formed JSON body that is not an object is rejected."""

    def _return_json_array(
        operation: cabc.Callable[[], bytes],
        *,
        description: str,
    ) -> bytes:
        """Return a JSON array instead of performing the read."""
        del operation, description
        return b"[]"

    monkeypatch.setattr(_github_http, "_with_retry", _return_json_array)

    with pytest.raises(TypeError, match="must be an object"):
        _load_json_response(url=_BOUNDED_URL, token=_BOUNDED_TOKEN)
