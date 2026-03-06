"""Unit tests for the benchmark baseline artifact fetch helper."""

from __future__ import annotations

import io
import typing as typ
import zipfile

import pytest

from benchmarks.fetch_main_benchmark_baseline import (
    MAIN_BASELINE_NOT_FOUND_EXIT_CODE,
    extract_artifact_archive,
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

    exit_code = main([
        "--repository",
        "leynos/cuprum",
        "--workflow",
        "ci.yml",
        "--artifact-name",
        "benchmark-ratchet-main-baseline",
        "--output-dir",
        str(tmp_path),
    ])

    assert exit_code == MAIN_BASELINE_NOT_FOUND_EXIT_CODE
