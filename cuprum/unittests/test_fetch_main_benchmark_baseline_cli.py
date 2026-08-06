"""Command-line surface tests for the benchmark baseline fetch helper."""

from __future__ import annotations

import io
import sys
import typing as typ
import zipfile

import pytest

from benchmarks.fetch_main_benchmark_baseline import (
    GITHUB_TOKEN_ENV_VAR,
    MAIN_BASELINE_NOT_FOUND_EXIT_CODE,
    _parse_args,
    main,
)
from cuprum.unittests._fetch_baseline_cli_support import ARTEFACT_NAME, main_cli_args

if typ.TYPE_CHECKING:
    import pathlib as pth

# The help text is asserted semantically rather than by snapshot: argparse
# rewrapped ``--artifact-name ARTEFACT_NAME`` between Python 3.12 and 3.13, so a
# stored transcript pins formatter behaviour rather than the CLI's contract.
EXPECTED_HELP_DESCRIPTION = (
    "Download the latest successful `main` benchmark baseline artefact."
)
EXPECTED_CLI_OPTIONS = (
    ("--repository", "GitHub repository in owner/name form."),
    ("--workflow", "Workflow file name or workflow identifier."),
    ("--artifact-name", "Artefact name to download from the latest successful run."),
    ("--output-dir", "Directory that receives the extracted artefact files."),
    ("--branch", "Branch to query for successful workflow runs."),
    ("--event", "Workflow event to query for successful runs."),
    ("--token-env", "Environment variable containing the GitHub token."),
)
REQUIRED_CLI_OPTIONS = ("--repository", "--workflow", "--artifact-name", "--output-dir")
ARGUMENT_ERROR_EXIT_CODE = 2


def _unwrapped(text: str) -> str:
    """Collapse runs of whitespace so assertions survive argparse rewrapping."""
    return " ".join(text.split())


def test_cli_help_documents_every_option(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--help`` should exit cleanly and describe each supported option."""
    monkeypatch.setattr(sys, "argv", ["fetch_main_benchmark_baseline.py"])
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    help_text = _unwrapped(capsys.readouterr().out)

    assert help_exit.value.code == 0
    assert _unwrapped(EXPECTED_HELP_DESCRIPTION) in help_text
    for option, description in EXPECTED_CLI_OPTIONS:
        assert option in help_text
        assert _unwrapped(description) in help_text


def test_cli_applies_optional_argument_defaults(tmp_path: pth.Path) -> None:
    """Optional arguments should keep their documented defaults."""
    arguments = _parse_args(main_cli_args(tmp_path))

    assert arguments.branch == "main"
    assert arguments.event == "push"
    assert arguments.token_env == GITHUB_TOKEN_ENV_VAR
    assert arguments.artefact_name == ARTEFACT_NAME


@pytest.mark.parametrize("omitted", REQUIRED_CLI_OPTIONS)
def test_cli_rejects_missing_required_option(
    omitted: str,
    tmp_path: pth.Path,
) -> None:
    """Each required option should be enforced by the parser."""
    argv = main_cli_args(tmp_path)
    index = argv.index(omitted)
    del argv[index : index + 2]

    with pytest.raises(SystemExit) as parse_exit:
        _parse_args(argv)

    assert parse_exit.value.code == ARGUMENT_ERROR_EXIT_CODE


def test_main_requires_github_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pth.Path,
) -> None:
    """CLI should fail fast, and name the variable, when the token is unset."""
    monkeypatch.delenv(GITHUB_TOKEN_ENV_VAR, raising=False)

    with pytest.raises(SystemExit) as token_exit:
        main(main_cli_args(tmp_path))

    assert str(token_exit.value) == (
        f"missing GitHub token in environment variable {GITHUB_TOKEN_ENV_VAR}"
    )


def test_main_returns_not_found_when_no_baseline_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pth.Path,
) -> None:
    """CLI should return the bootstrap exit code when no baseline is found."""
    monkeypatch.setenv(GITHUB_TOKEN_ENV_VAR, "token")
    monkeypatch.setattr(
        "benchmarks.fetch_main_benchmark_baseline.find_latest_artefact_download_url",
        lambda **_: None,
    )

    exit_code = main(main_cli_args(tmp_path))

    assert exit_code == MAIN_BASELINE_NOT_FOUND_EXIT_CODE


def test_main_downloads_and_extracts_latest_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pth.Path,
) -> None:
    """CLI should download the selected archive and extract it into output_dir."""
    monkeypatch.setenv(GITHUB_TOKEN_ENV_VAR, "token")
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

    exit_code = main(main_cli_args(tmp_path))

    assert exit_code == 0
    assert (tmp_path / "main-plan.json").read_text(encoding="utf-8") == (
        '{"dry_run": true}'
    )
    assert (tmp_path / "main-throughput.json").read_text(encoding="utf-8") == (
        '{"results": []}'
    )
