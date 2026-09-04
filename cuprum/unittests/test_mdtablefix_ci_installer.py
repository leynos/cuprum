"""Process-boundary contracts for CI's mdtablefix installer branches."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - executes the checked-in CI script.
import typing as typ

import pytest

from tests.helpers.workflow import Workflow, step_named

if typ.TYPE_CHECKING:
    import pathlib as pth


class _InstallerCase(typ.NamedTuple):
    """Describe one mdtablefix installer branch."""

    cargo_binstall_status: int
    expected_install: str
    unexpected_install: str


def _installer_script(workflow_data: Workflow) -> str:
    """Return the checked-in mdtablefix installer script."""
    step = step_named(workflow_data, "lint-test", "Install mdtablefix")
    script = step.get("run")
    assert isinstance(script, str), "the Install mdtablefix CI step must run a script"
    return script


def _write_command_stub(directory: pth.Path, name: str, body: str) -> pth.Path:
    """Create an executable command stub with the given shell body."""
    command = directory / name
    command.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    command.chmod(0o755)
    return command


def _install_command_stubs(directory: pth.Path) -> None:
    """Install the controlled cargo, rustup, and mdtablefix command stubs."""
    _write_command_stub(
        directory,
        "cargo",
        """\
printf 'cargo' >> "$INSTALLER_LOG"
printf ' %s' "$@" >> "$INSTALLER_LOG"
printf '\\n' >> "$INSTALLER_LOG"
if [ "$1" = binstall ] && [ "$2" = -V ]; then
  exit "$CARGO_BINSTALL_STATUS"
fi
if [ "$1" = binstall ] || { [ "$1" = +1.89.0 ] && [ "$2" = install ]; }; then
  printf '0.5.0' > "$MDTABLEFIX_STATE"
fi
""",
    )
    _write_command_stub(
        directory,
        "rustup",
        """\
printf 'rustup' >> "$INSTALLER_LOG"
printf ' %s' "$@" >> "$INSTALLER_LOG"
printf '\\n' >> "$INSTALLER_LOG"
""",
    )
    _write_command_stub(
        directory,
        "mdtablefix",
        """\
printf 'mdtablefix' >> "$INSTALLER_LOG"
printf ' %s' "$@" >> "$INSTALLER_LOG"
printf '\\n' >> "$INSTALLER_LOG"
if [ -f "$MDTABLEFIX_STATE" ]; then
  version=$(cat "$MDTABLEFIX_STATE")
else
  version=0.4.0
fi
printf 'mdtablefix %s\\n' "$version"
""",
    )


def _run_installer(
    temporary_directory: pth.Path,
    script: str,
    *,
    cargo_binstall_status: int,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run one installer branch with controlled command availability."""
    commands_directory = temporary_directory / "commands"
    commands_directory.mkdir()
    _install_command_stubs(commands_directory)
    installer = temporary_directory / "install-mdtablefix.sh"
    installer.write_text(script, encoding="utf-8")
    log = temporary_directory / "installer.log"
    state = temporary_directory / "mdtablefix-version"
    bash = shutil.which("bash")
    assert bash is not None, "the installer contract test requires Bash"
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - executes the checked-in CI script.
        [bash, str(installer)],
        check=False,
        capture_output=True,
        encoding="utf-8",
        env=os.environ
        | {
            "CARGO_BINSTALL_STATUS": str(cargo_binstall_status),
            "INSTALLER_LOG": str(log),
            "MDTABLEFIX_STATE": str(state),
            "MDTABLEFIX_VERSION": "0.5.0",
            "MDTABLEFIX_RUST_VERSION": "1.89.0",
            "PATH": f"{commands_directory}{os.pathsep}{os.environ['PATH']}",
        },
    )
    return result, log.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _InstallerCase(
                cargo_binstall_status=0,
                expected_install=(
                    "cargo binstall --no-confirm --locked mdtablefix@0.5.0"
                ),
                unexpected_install="rustup toolchain install --profile minimal 1.89.0",
            ),
            id="prebuilt-binstall",
        ),
        pytest.param(
            _InstallerCase(
                cargo_binstall_status=1,
                expected_install="rustup toolchain install --profile minimal 1.89.0",
                unexpected_install=(
                    "cargo binstall --no-confirm --locked mdtablefix@0.5.0"
                ),
            ),
            id="rust-source-fallback",
        ),
    ],
)
def test_mdtablefix_installer_uses_the_available_install_path(
    workflow_data: Workflow,
    tmp_path: pth.Path,
    *,
    case: _InstallerCase,
) -> None:
    """The CI installer selects binstall or the dedicated Rust fallback."""
    result, calls = _run_installer(
        tmp_path,
        _installer_script(workflow_data),
        cargo_binstall_status=case.cargo_binstall_status,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "cargo binstall -V" in calls, (
        "the installer must probe cargo binstall with its supported -V command"
    )
    assert case.expected_install in calls, (
        f"the installer must execute {case.expected_install!r}; found {calls!r}"
    )
    assert case.unexpected_install not in calls, (
        f"the installer must not execute {case.unexpected_install!r}; found {calls!r}"
    )
    if case.cargo_binstall_status != 0:
        assert "cargo +1.89.0 install --locked mdtablefix --version 0.5.0" in calls, (
            "the fallback must build mdtablefix with the dedicated Rust 1.89.0 "
            "toolchain"
        )
    assert calls[-1] == "mdtablefix --version", (
        "the installer must verify the installed mdtablefix version after installation"
    )
