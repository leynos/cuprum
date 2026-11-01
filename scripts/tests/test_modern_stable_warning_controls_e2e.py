"""Exercise the warning-control command end to end in a fixture repository.

The contract tests call the orchestration with injected boundaries. This test
runs the shipped entry point instead: it initialises a throwaway Git repository
whose Makefile checks one tiny crate, then lets the command create, exercise,
and remove its own worktree. Only the scale is reduced; every Git, Make, Cargo,
file, and cleanup operation is the one the real run performs.

The fixture stays under the Rust workspace's target directory because a build
artefact does not belong in `/tmp`.
"""

from __future__ import annotations

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Git fixture argv.
import tempfile
import typing as typ
from pathlib import Path

from scripts.modern_stable_warning_controls import ControlHarness, main
from scripts.modern_stable_warning_observation import Gate
from tests.helpers.cargo_warning_fixture import (
    MODERN_STABLE_TOOLCHAIN,
    require_toolchain,
)
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import pathlib as pth

#: Fixture repositories stay in the Rust workspace, never under `/tmp`.
E2E_PARENT = repo_root() / "rust/target/modern-stable-warning-controls-e2e"
#: Absolute Git path so the fixture never relies on a partial executable path.
GIT = shutil.which("git")

PHASES = (
    "warning-cold",
    "warning-prewarm",
    "warning-warm",
    "warning-warm-repeat",
    "clean",
)

MAKEFILE = (
    "MODERN_STABLE_ENV = CARGO_BUILD_WARNINGS=deny CARGO_TERM_COLOR=never "
    "CARGO_TARGET_DIR=$(CURDIR)/target RUSTUP_TOOLCHAIN="
    + MODERN_STABLE_TOOLCHAIN
    + "\n"
    "\n"
    "check:\n"
    "\t$(MODERN_STABLE_ENV) cargo check --locked --offline --verbose "
    "--manifest-path crate/Cargo.toml\n"
)

MANIFEST = (
    "[package]\n"
    'name = "warning-control-fixture"\n'
    'version = "0.1.0"\n'
    'edition = "2024"\n'
    "\n"
    "[workspace]\n"
)

LOCKFILE = (
    'version = 4\n\n[[package]]\nname = "warning-control-fixture"\nversion = "0.1.0"\n'
)

LIB_SOURCE = "//! Fixture crate for the warning-control command.\n"


def _git(repository: pth.Path, *arguments: str) -> None:
    """Run one fixed Git command inside the fixture repository."""
    assert GIT is not None, "the warning-control fixture requires Git"
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Git fixture argv.
        [GIT, "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _fixture_repository(root: pth.Path) -> Gate:
    """Create and commit a one-crate repository, returning its gate."""
    crate = root / "crate"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(MANIFEST, encoding="utf-8")
    (crate / "Cargo.lock").write_text(LOCKFILE, encoding="utf-8")
    (crate / "src/lib.rs").write_text(LIB_SOURCE, encoding="utf-8")
    (root / "Makefile").write_text(MAKEFILE, encoding="utf-8")
    _git(root, "init", "--quiet")
    _git(root, "add", "--all")
    _git(
        root,
        "-c",
        "user.name=Warning Control Fixture",
        "-c",
        "user.email=warning-control@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    return Gate("check", "check", "warning-control-fixture", Path("crate/src/lib.rs"))


def test_the_command_runs_every_phase_and_cleans_up() -> None:
    """Prove the real entry point exercises a worktree and removes it again."""
    require_toolchain()
    E2E_PARENT.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="controller-", dir=E2E_PARENT) as area:
        root = Path(area) / "repository"
        root.mkdir()
        gate = _fixture_repository(root)

        exit_code = main(root=root, harness=ControlHarness(gates=(gate,)))

        report_path = root / "rust/target/modern-stable-warning-controls/report.md"
        report = report_path.read_text(encoding="utf-8")
        assert exit_code == 0, report
        assert "Failures:\n\n- none" in report, (
            "every control phase must report its expected result"
        )
        assert all(f"| {phase} |" in report for phase in PHASES), (
            "the report must retain one row per control phase"
        )
        assert "| Compiling |" in report or "| Checking |" in report, (
            "the clean rerun must rebuild the package after the warning is removed"
        )

        log_directory = report_path.parent
        assert sorted(path.name for path in log_directory.glob("*.log")) == sorted(
            f"check-{phase}.log" for phase in PHASES
        ), "every phase must retain its own Cargo log"

        assert not list(Path(area).glob(".modern-stable-warning-controls-*")), (
            "the command must remove the worktree it created"
        )
        assert (root / gate.source).read_text(encoding="utf-8") == LIB_SOURCE, (
            "the command must leave its own repository's source untouched"
        )
