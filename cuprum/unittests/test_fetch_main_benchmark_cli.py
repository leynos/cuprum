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


@pytest.fixture
def main_cli_args(tmp_path: pth.Path) -> list[str]:
    """Return CLI arguments for invoking the baseline fetch command."""
    return [
        "--repository",
        "leynos/cuprum",
        "--workflow",
        "ci.yml",
        "--artifact-name",
        "benchmark-ratchet-main-baseline",
        "--output-dir",
        str(tmp_path),
    ]


def test_main_returns_not_found_when_no_baseline_available(
    monkeypatch: pytest.MonkeyPatch,
    main_cli_args: list[str],
) -> None:
    """CLI should return the bootstrap exit code when no baseline is found."""
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        "benchmarks.fetch_main_benchmark_baseline.find_latest_artefact_download_url",
        lambda **_: None,
    )

    exit_code = main(main_cli_args)

    assert exit_code == MAIN_BASELINE_NOT_FOUND_EXIT_CODE, (
        "missing baseline should return the not-found exit code"
    )


def test_main_requires_github_token(
    monkeypatch: pytest.MonkeyPatch,
    main_cli_args: list[str],
) -> None:
    """CLI should fail fast when the configured token env var is unset."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(SystemExit, match="missing GitHub token"):
        main(main_cli_args)


def test_main_downloads_and_extracts_latest_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pth.Path,
    main_cli_args: list[str],
) -> None:
    """CLI should download the selected archive and extract it into output_dir."""
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        "benchmarks.fetch_main_benchmark_baseline.find_latest_artefact_download_url",
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

    exit_code = main(main_cli_args)

    assert exit_code == 0, "successful extraction should return exit code zero"
    assert (tmp_path / "main-plan.json").read_text(encoding="utf-8") == (
        '{"dry_run": true}'
    ), "the extracted plan should preserve its archive content"
    assert (tmp_path / "main-throughput.json").read_text(encoding="utf-8") == (
        '{"results": []}'
    ), "the extracted throughput file should preserve its archive content"
