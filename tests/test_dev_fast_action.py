"""Execute the Linux dev-fast composite action under controlled shell fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.helpers.composite_actions import StepResult, run_step, step_script

ACTION = ".github/actions/setup-dev-fast"
INSTALL_STEP = "Install the pinned backend and linker"
MOLD_VERSION = "2.41.0"
CHECKSUM = "a" * 64


def _write_program(directory: Path, name: str, body: str) -> None:
    """Write one controlled shell command for an action-step test."""
    program = directory / name
    program.write_text(f"#!/usr/bin/env bash\nset -eu\n{body}\n", encoding="utf-8")
    program.chmod(0o755)


def _write_mold_metadata(directory: Path, checksum_lines: str) -> None:
    """Create the action's repository-relative linker metadata."""
    mold = directory / "tools/mold"
    mold.mkdir(parents=True)
    (mold / "VERSION").write_text(f"{MOLD_VERSION}\n", encoding="utf-8")
    (mold / "SHA256SUMS").write_text(checksum_lines, encoding="utf-8")


def _action_environment(
    tmp_path: Path, architecture: str, *, sha_status: int = 0
) -> dict[str, str]:
    """Install fake trusted commands and return the emulated runner environment."""
    commands = tmp_path / "bin"
    commands.mkdir()
    records = tmp_path / "records"
    records.mkdir()
    _write_program(commands, "uname", f"printf '%s\\n' '{architecture}'")
    _write_program(
        commands, "rustup", 'printf \'%s\\n\' "$@" >> "$DEV_FAST_RECORDS/rustup"'
    )
    _write_program(
        commands,
        "curl",
        "output=''\n"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = --output ]; then output="$2"; shift 2; continue; fi\n'
        '  printf \'%s\\n\' "$1" >> "$DEV_FAST_RECORDS/curl"\n'
        "  shift\n"
        "done\n"
        'mkdir -p "$(dirname "$output")"\n'
        'printf archive > "$output"',
    )
    _write_program(
        commands,
        "sha256sum",
        'cat > "$DEV_FAST_RECORDS/checksum"\nexit "$DEV_FAST_SHA_STATUS"',
    )
    _write_program(commands, "tar", 'printf \'%s\\n\' "$@" > "$DEV_FAST_RECORDS/tar"')
    github_path = tmp_path / "github_path"
    return {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "DEV_FAST_RECORDS": str(records),
        "DEV_FAST_SHA_STATUS": str(sha_status),
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
        "GITHUB_PATH": str(github_path),
    }


def _run_install(tmp_path: Path, environment: dict[str, str]) -> StepResult:
    """Execute the action's installation step with the supplied fake tools."""
    return run_step(
        step_script(ACTION, INSTALL_STEP), workdir=tmp_path, environment=environment
    )


@pytest.mark.parametrize(
    ("architecture", "archive"),
    [
        pytest.param("x86_64", "mold-2.41.0-x86_64-linux.tar.gz", id="x86_64"),
        pytest.param("aarch64", "mold-2.41.0-aarch64-linux.tar.gz", id="aarch64"),
    ],
)
def test_install_selects_the_pinned_archive_and_exports_its_path(
    tmp_path: Path, architecture: str, archive: str
) -> None:
    """Supported architectures download, check, extract, and expose one archive."""
    _write_mold_metadata(tmp_path, f"{CHECKSUM}  {archive}\n")
    environment = _action_environment(tmp_path, architecture)
    result = _run_install(tmp_path, environment)
    records = Path(environment["DEV_FAST_RECORDS"])
    assert result.returncode == 0, result.stderr
    assert records.joinpath("rustup").read_text(encoding="utf-8").splitlines() == [
        "toolchain",
        "install",
        "nightly-2026-08-23",
        "--profile",
        "minimal",
        "component",
        "add",
        "rustc-codegen-cranelift",
        "clippy",
        "--toolchain",
        "nightly-2026-08-23",
    ], "the action must provision only the pinned nightly components"
    assert (
        records
        .joinpath("curl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
        .endswith(f"/v{MOLD_VERSION}/{archive}")
    ), "the action must fetch the selected upstream archive"
    assert records.joinpath("checksum").read_text(encoding="utf-8") == (
        f"{CHECKSUM}  {environment['RUNNER_TEMP']}/{archive}\n"
    ), "the action must verify the pinned checksum against its downloaded archive"
    tar_arguments = records.joinpath("tar").read_text(encoding="utf-8").splitlines()
    assert tar_arguments == [
        "--extract",
        "--gzip",
        "--strip-components=1",
        "--directory",
        f"{tmp_path}/.local",
        "--file",
        f"{environment['RUNNER_TEMP']}/{archive}",
    ], "the action must extract the verified archive into the local tool path"
    assert Path(environment["GITHUB_PATH"]).read_text(encoding="utf-8") == (
        f"{tmp_path}/.local/bin\n"
    ), "the action must expose the installed linker to later workflow steps"


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_install_accepts_github_runner_architecture_aliases(
    tmp_path: Path, architecture: str
) -> None:
    """GitHub runner architecture aliases map to their canonical mold archive."""
    canonical = "x86_64" if architecture == "amd64" else "aarch64"
    archive = f"mold-{MOLD_VERSION}-{canonical}-linux.tar.gz"
    _write_mold_metadata(tmp_path, f"{CHECKSUM}  {archive}\n")
    environment = _action_environment(tmp_path, architecture)
    result = _run_install(tmp_path, environment)
    assert result.returncode == 0, result.stderr


def test_install_rejects_an_unsupported_architecture_before_provisioning(
    tmp_path: Path,
) -> None:
    """An unsupported runner fails before installing or downloading anything."""
    _write_mold_metadata(tmp_path, "")
    environment = _action_environment(tmp_path, "riscv64")
    result = _run_install(tmp_path, environment)
    records = Path(environment["DEV_FAST_RECORDS"])
    assert result.returncode != 0, "unsupported architecture must fail closed"
    assert "unsupported mold architecture: riscv64" in result.stderr
    assert not records.joinpath("rustup").exists(), (
        "unsupported hosts must not install nightly"
    )
    assert not records.joinpath("curl").exists(), (
        "unsupported hosts must not download mold"
    )


@pytest.mark.parametrize(
    "checksum_lines",
    [
        pytest.param("", id="zero_entries"),
        pytest.param(
            "\n".join([
                f"{CHECKSUM}  mold-2.41.0-x86_64-linux.tar.gz",
                f"{'b' * 64}  mold-2.41.0-x86_64-linux.tar.gz",
                "",
            ]),
            id="multiple_entries",
        ),
    ],
)
def test_install_requires_exactly_one_checksum_entry(
    tmp_path: Path, checksum_lines: str
) -> None:
    """The action refuses missing and ambiguous checksum metadata before download."""
    _write_mold_metadata(tmp_path, checksum_lines)
    environment = _action_environment(tmp_path, "x86_64")
    result = _run_install(tmp_path, environment)
    records = Path(environment["DEV_FAST_RECORDS"])
    assert result.returncode != 0, "checksum metadata must identify exactly one archive"
    assert not records.joinpath("curl").exists(), (
        "ambiguous checksums must stop download"
    )


def test_install_propagates_a_download_failure(tmp_path: Path) -> None:
    """A failed curl command stops before checksum verification or extraction."""
    archive = "mold-2.41.0-x86_64-linux.tar.gz"
    _write_mold_metadata(tmp_path, f"{CHECKSUM}  {archive}\n")
    environment = _action_environment(tmp_path, "x86_64")
    commands = Path(environment["PATH"].split(":", maxsplit=1)[0])
    _write_program(commands, "curl", "printf 'download failed\\n' >&2\nexit 22")
    result = _run_install(tmp_path, environment)
    records = Path(environment["DEV_FAST_RECORDS"])
    assert result.returncode == 22, "curl's failure status must fail the action"
    assert "download failed" in result.stderr
    assert not records.joinpath("checksum").exists(), (
        "failed downloads must not be checked"
    )
    assert not records.joinpath("tar").exists(), (
        "failed downloads must not be extracted"
    )


def test_install_propagates_a_checksum_mismatch(tmp_path: Path) -> None:
    """A checksum failure stops before archive extraction or path export."""
    archive = "mold-2.41.0-x86_64-linux.tar.gz"
    _write_mold_metadata(tmp_path, f"{CHECKSUM}  {archive}\n")
    environment = _action_environment(tmp_path, "x86_64", sha_status=1)
    result = _run_install(tmp_path, environment)
    records = Path(environment["DEV_FAST_RECORDS"])
    assert result.returncode == 1, "checksum mismatch must fail the action"
    assert records.joinpath("checksum").exists(), (
        "the downloaded archive must be checked"
    )
    assert not records.joinpath("tar").exists(), (
        "mismatched archives must not be extracted"
    )
    assert not Path(environment["GITHUB_PATH"]).exists(), (
        "a rejected archive must not expose an installation path"
    )
