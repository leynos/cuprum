"""Unit tests for the benchmark GitHub HTTP transport."""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from unittest import mock

import pytest

from benchmarks._github_http import _with_retry
from benchmarks.fetch_main_benchmark_baseline import _load_json_response


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

    monkeypatch.setattr(
        "benchmarks._github_http.urllib.request.build_opener",
        lambda *_: _Opener(),
    )
    monkeypatch.setattr("benchmarks._github_http._MAX_JSON_RESPONSE_BYTES", 4)

    with pytest.raises(ValueError, match=r"JSON response .* exceeds 4 bytes"):
        _load_json_response(
            url="https://example.invalid/workflow-runs",
            token="".join(("tok", "en")),
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
    assert delays == [], "non-transient errors should not schedule a retry delay"
