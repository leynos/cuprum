"""Unit tests for the benchmark baseline fetch command-line interface."""

from __future__ import annotations

import io
import typing as typ
import zipfile

import pytest

from benchmarks.fetch_main_benchmark_baseline import (
    MAIN_BASELINE_NOT_FOUND_EXIT_CODE,
    main,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


def _main_cli_args(output_dir: pth.Path) -> list[str]:
    """Return CLI arguments for invoking the baseline fetch command."""
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
