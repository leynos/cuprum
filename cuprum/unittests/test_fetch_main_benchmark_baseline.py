"""Unit tests for the benchmark baseline artifact fetch helper."""

from __future__ import annotations

import io
import typing as typ
import urllib.error
import zipfile

import pytest

from benchmarks.fetch_main_benchmark_baseline import (
    MAIN_BASELINE_NOT_FOUND_EXIT_CODE,
    ArtifactQuery,
    _load_json_response,
    extract_artifact_archive,
    find_latest_artifact_download_url,
    main,
    select_latest_artifact_download_url,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


def _workflow_runs_payload(*run_ids: int) -> dict[str, object]:
    return {
        "workflow_runs": [{"id": run_id} for run_id in run_ids],
    }


def _artifacts_payload(*, artifacts: list[dict[str, object]]) -> dict[str, object]:
    return {"artifacts": artifacts}


def _main_cli_args(output_dir: pth.Path) -> list[str]:
    return [
        "--repository",
        "leynos/cuprum",
        "--workflow",
        "ci.yml",
        "--artifact-name",
        "benchmark-ratchet-main-baseline",
        "--output-dir",
        str(output_dir),
    ]


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


def test_main_returns_not_found_when_no_baseline_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pth.Path,
) -> None:
    """CLI should return the bootstrap exit code when no baseline is found."""
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        "benchmarks.fetch_main_benchmark_baseline.find_latest_artifact_download_url",
        lambda **_: None,
    )

    exit_code = main(_main_cli_args(tmp_path))

    assert exit_code == MAIN_BASELINE_NOT_FOUND_EXIT_CODE


def test_main_requires_github_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pth.Path,
) -> None:
    """CLI should fail fast when the configured token env var is unset."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(SystemExit, match="missing GitHub token"):
        main(_main_cli_args(tmp_path))


def test_main_downloads_and_extracts_latest_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pth.Path,
) -> None:
    """CLI should download the selected archive and extract it into output_dir."""
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        "benchmarks.fetch_main_benchmark_baseline.find_latest_artifact_download_url",
        lambda **_: "https://example.invalid/baseline.zip",
    )

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, mode="w") as archive:
        archive.writestr("main-plan.json", '{"dry_run": true}')
        archive.writestr("main-throughput.json", '{"results": []}')

    monkeypatch.setattr(
        "benchmarks.fetch_main_benchmark_baseline._download_bytes",
        lambda **_: archive_buffer.getvalue(),
    )

    exit_code = main(_main_cli_args(tmp_path))

    assert exit_code == 0
    assert (tmp_path / "main-plan.json").read_text(encoding="utf-8") == (
        '{"dry_run": true}'
    )
    assert (tmp_path / "main-throughput.json").read_text(encoding="utf-8") == (
        '{"results": []}'
    )


def test_load_json_response_retries_transient_urlopen_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient transport failures should be retried with a bounded loop."""
    temporary_outage = "temporary outage"
    auth_token = "".join(("tok", "en"))

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"workflow_runs": []}'

    attempts = 0
    timeouts: list[float] = []

    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        nonlocal attempts
        del request
        attempts += 1
        timeouts.append(timeout)
        if attempts < 3:
            raise urllib.error.URLError(temporary_outage)
        return _Response()

    monkeypatch.setattr(
        "benchmarks.fetch_main_benchmark_baseline.urllib.request.urlopen",
        fake_urlopen,
    )
    monkeypatch.setattr(
        "benchmarks.fetch_main_benchmark_baseline.time.sleep", lambda _: None
    )

    payload = _load_json_response(
        url="https://example.invalid/workflow-runs",
        token=auth_token,
    )

    assert payload == {"workflow_runs": []}
    assert attempts == 3
    assert timeouts == [10.0, 10.0, 10.0]


def test_find_latest_artifact_download_url_queries_workflow_and_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact lookup should fetch workflow runs and then per-run artifacts."""
    auth_token = "".join(("tok", "en"))
    payloads: list[typ.Mapping[str, object]] = [
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

    def fake_load_json_response(*, url: str, token: str) -> typ.Mapping[str, object]:
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
