"""Unit tests for the benchmark baseline artifact fetch helper."""

from __future__ import annotations

import http.client
import io
import math
import typing as typ
import urllib.error
import urllib.request
import zipfile
from unittest import mock

import pytest

from benchmarks._github_http import _ArtifactArchiveRedirectHandler, _with_retry
from benchmarks.fetch_main_benchmark_baseline import (
    ArtifactQuery,
    _download_bytes,
    _load_json_response,
    extract_artifact_archive,
    find_latest_artifact_download_url,
    select_latest_artifact_download_url,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth


def _workflow_runs_payload(*run_ids: int) -> dict[str, object]:
    """Return a stub GitHub workflow-runs API payload."""
    return {
        "workflow_runs": [{"id": run_id} for run_id in run_ids],
    }


def _artifacts_payload(*, artifacts: list[dict[str, object]]) -> dict[str, object]:
    """Return a stub GitHub run-artifacts API payload."""
    return {"artifacts": artifacts}


def test_select_latest_artifact_download_url_uses_newest_matching_run() -> None:
    """Artifact selection should prefer the newest run with a valid baseline."""
    runs_payload = _workflow_runs_payload(300, 200, 100)
    artifacts_by_run = {
        300: _artifacts_payload(
            artifacts=[
                {
                    "name": "benchmark-ratchet-main-baseline",
                    "expired": True,
                    "archive_download_url": "https://example.invalid/expired.zip",
                }
            ]
        ),
        200: _artifacts_payload(
            artifacts=[
                {
                    "name": "benchmark-ratchet-main-baseline",
                    "expired": False,
                    "archive_download_url": "https://example.invalid/valid.zip",
                }
            ]
        ),
        100: _artifacts_payload(
            artifacts=[
                {
                    "name": "benchmark-ratchet-main-baseline",
                    "expired": False,
                    "archive_download_url": "https://example.invalid/older.zip",
                }
            ]
        ),
    }

    download_url = select_latest_artifact_download_url(
        workflow_runs_payload=runs_payload,
        artifacts_payload_by_run=artifacts_by_run,
        artifact_name="benchmark-ratchet-main-baseline",
    )

    assert download_url == "https://example.invalid/valid.zip"


def test_select_latest_artifact_download_url_returns_none_without_match() -> None:
    """Artifact selection should report no baseline when nothing matches."""
    runs_payload = _workflow_runs_payload(200, 100)
    artifacts_by_run = {
        200: _artifacts_payload(
            artifacts=[
                {
                    "name": "coverage",
                    "expired": False,
                    "archive_download_url": "https://example.invalid/coverage.zip",
                }
            ]
        ),
        100: _artifacts_payload(artifacts=[]),
    }

    download_url = select_latest_artifact_download_url(
        workflow_runs_payload=runs_payload,
        artifacts_payload_by_run=artifacts_by_run,
        artifact_name="benchmark-ratchet-main-baseline",
    )

    assert download_url is None


def test_extract_artifact_archive_unpacks_json_files(tmp_path: pth.Path) -> None:
    """Artifact extraction should unpack zip members into the output directory."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        archive.writestr("main-plan.json", '{"dry_run": true}')
        archive.writestr("main-throughput.json", '{"results": []}')

    extracted = extract_artifact_archive(
        archive_bytes=buffer.getvalue(),
        output_dir=tmp_path,
    )

    assert [path.name for path in extracted] == [
        "main-plan.json",
        "main-throughput.json",
    ]
    assert (tmp_path / "main-plan.json").read_text(encoding="utf-8") == (
        '{"dry_run": true}'
    )


def test_extract_artifact_archive_rejects_path_traversal(tmp_path: pth.Path) -> None:
    """Artifact extraction must reject archive members escaping output_dir."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        archive.writestr("../escape.json", '{"unsafe": true}')

    with pytest.raises(ValueError, match="archive member path"):
        extract_artifact_archive(
            archive_bytes=buffer.getvalue(),
            output_dir=tmp_path,
        )


def test_load_json_response_retries_transient_urlopen_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient transport failures should be retried with a bounded loop."""
    temporary_outage = "temporary outage"
    auth_token = "".join(("tok", "en"))

    class _Response:
        """Minimal stub of an HTTP response context manager."""

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

        @staticmethod
        def read() -> bytes:
            """Return canned JSON bytes for the workflow-runs response."""
            return b'{"workflow_runs": []}'

    attempts = 0
    timeouts: list[float] = []

    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        """Fail twice then return the stub response, recording timeouts.

        Raises
        ------
        urllib.error.URLError
            On the first two calls, to exercise the retry loop.
        """  # noqa: DOC201 - summary states the return; Raises documents the retry
        nonlocal attempts
        del request
        attempts += 1
        timeouts.append(timeout)
        if attempts < 3:
            raise urllib.error.URLError(temporary_outage)
        return _Response()

    monkeypatch.setattr(
        "benchmarks._github_http.urllib.request.urlopen",
        fake_urlopen,
    )
    monkeypatch.setattr("benchmarks._github_http.time.sleep", lambda _: None)

    payload = _load_json_response(
        url="https://example.invalid/workflow-runs",
        token=auth_token,
    )

    assert payload == {"workflow_runs": []}
    assert attempts == 3
    assert timeouts == [10.0, 10.0, 10.0]


def test_with_retry_returns_after_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient failures should use both delays before succeeding."""
    operation = mock.Mock(
        side_effect=[
            urllib.error.URLError("first"),
            urllib.error.URLError("second"),
            "done",
        ]
    )
    delays: list[float] = []
    monkeypatch.setattr("benchmarks._github_http.time.sleep", delays.append)

    result = _with_retry(operation, description="test")

    assert result == "done"
    assert operation.call_count == 3
    assert delays == [0.5, 1.0]


def test_with_retry_raises_non_transient_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-transient HTTP status should fail without sleeping."""
    error = urllib.error.HTTPError(
        "https://example.invalid",
        404,
        "missing",
        http.client.HTTPMessage(),
        None,
    )
    operation = mock.Mock(side_effect=error)
    delays: list[float] = []
    monkeypatch.setattr("benchmarks._github_http.time.sleep", delays.append)

    with pytest.raises(urllib.error.HTTPError) as raised:
        _with_retry(operation, description="test")

    assert raised.value is error
    assert operation.call_count == 1
    assert delays == []


def test_with_retry_raises_final_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausting the schedule should raise the final request failure."""
    errors = [urllib.error.URLError(reason) for reason in ("one", "two", "three")]
    operation = mock.Mock(side_effect=errors)
    delays: list[float] = []
    monkeypatch.setattr("benchmarks._github_http.time.sleep", delays.append)

    with pytest.raises(urllib.error.URLError) as raised:
        _with_retry(operation, description="test")

    assert raised.value is errors[-1]
    assert operation.call_count == 3
    assert delays == [0.5, 1.0]


def test_download_bytes_uses_artifact_redirect_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact downloads should use the redirect policy that strips auth."""

    class _Response:
        """Minimal stub of an HTTP response context manager."""

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

        @staticmethod
        def read() -> bytes:
            """Return canned archive bytes for the download response."""
            return b"archive-bytes"

    class _Opener:
        """Minimal stub of a urllib opener returning the stub response."""

        @staticmethod
        def open(
            request: urllib.request.Request,
            *,
            timeout: float,
        ) -> _Response:
            """Assert request auth and timeout, then return the stub response."""
            assert request.get_header("Authorization") == "Bearer token"
            assert math.isclose(timeout, 10.0)
            return _Response()

    def fake_build_opener(
        *handlers: urllib.request.BaseHandler,
    ) -> _Opener:
        """Assert the redirect handler is installed, then return the stub opener."""
        assert any(
            isinstance(handler, _ArtifactArchiveRedirectHandler) for handler in handlers
        )
        return _Opener()

    monkeypatch.setattr(
        "benchmarks._github_http.urllib.request.build_opener",
        fake_build_opener,
    )

    archive_bytes = _download_bytes(
        url="https://api.github.com/repos/leynos/cuprum/actions/artifacts/1/zip",
        token="".join(("tok", "en")),
    )

    assert archive_bytes == b"archive-bytes"


def test_find_latest_artifact_download_url_queries_workflow_and_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact lookup should fetch workflow runs and then per-run artifacts."""
    auth_token = "".join(("tok", "en"))
    payloads: list[cabc.Mapping[str, object]] = [
        {"workflow_runs": [{"id": 42}]},
        {
            "artifacts": [
                {
                    "name": "benchmark-ratchet-main-baseline",
                    "expired": False,
                    "archive_download_url": "https://example.invalid/archive.zip",
                }
            ]
        },
    ]
    requested_urls: list[str] = []

    def fake_load_json_response(*, url: str, token: str) -> cabc.Mapping[str, object]:
        """Record the requested URL and return the next queued payload."""
        del token
        requested_urls.append(url)
        return payloads.pop(0)

    monkeypatch.setattr(
        "benchmarks.fetch_main_benchmark_baseline._load_json_response",
        fake_load_json_response,
    )

    download_url = find_latest_artifact_download_url(
        query=ArtifactQuery(
            repository="leynos/cuprum",
            workflow="ci.yml",
            branch="main",
            event="push",
            artifact_name="benchmark-ratchet-main-baseline",
        ),
        token=auth_token,
    )

    assert download_url == "https://example.invalid/archive.zip"
    assert len(requested_urls) == 2
    assert requested_urls[0].endswith(
        "/actions/workflows/ci.yml/runs?branch=main&event=push&per_page=20&status=success"
    )
    assert requested_urls[1].endswith("/actions/runs/42/artifacts?per_page=100")
