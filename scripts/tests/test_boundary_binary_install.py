"""The Make install targets must delegate verifier binaries safely.

Kani and Z3 installation is owned by the shared `rust-prover-tools` dependency,
so these tests run the real Make recipes against a fake CLI and a fake Cargo.
That observes the exact command line, environment, and ordering without network
access, and it keeps the retired local installers from creeping back. The
fakes, the runner, and the Makefile readers live in the colocated support
module.
"""

from __future__ import annotations

import json
import os
import typing as typ

import pytest

from scripts.tests.boundary_binary_install_support import (
    AMBIENT_INSTALL_VARIABLES,
    FAKE_FRONTEND,
    FAKE_UV,
    MAKEFILE,
    REPO_ROOT,
    SPACED_ROOT_NAME,
    FakeTools,
    MakeCall,
    ambient_guard_names,
    makefile_assignment,
    recorded_argv,
    recorded_values,
    run_make,
    write_executable,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Create an isolated checkout path that contains spaces."""
    path = tmp_path / SPACED_ROOT_NAME
    path.mkdir()
    return path


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    """Create the fake tool executables outside the spaced checkout path."""
    return FakeTools(tmp_path / "fakes")


def test_kani_install_delegates_download_setup_and_verification(
    workdir: Path, fakes: FakeTools
) -> None:
    """Kani installs through the shared CLI, then performs exactly one setup."""
    kani_version = makefile_assignment("KANI_VERSION")
    pin = REPO_ROOT / "tools/kani/VERSION"
    assert pin.read_text(encoding="utf-8").strip() == kani_version, (
        "the setup bundle path must name the version the updater actually pins"
    )

    result = run_make(workdir, MakeCall("install-boundary-kani"), fakes)
    assert result.returncode == 0, result.stderr

    records = fakes.records()
    assert [record["tool"] for record in records] == ["prover-tools", "cargo"], (
        "the download must precede setup, and setup must run exactly once"
    )
    cache = workdir / ".cache/boundary-kani"
    expected_install = [
        "kani",
        "install",
        "--repo-root",
        str(workdir),
        "--version-file",
        "tools/kani/VERSION",
        "--install-dir",
        str(cache / "bin"),
        "--cache-dir",
        str(cache),
        "--no-compile",
        "--no-setup",
        "--no-verify",
    ]
    assert records[0]["argv"] == expected_install, (
        "the delegated Kani install lost its explicit paths or safety flags"
    )
    expected_setup = [
        "kani",
        "setup",
        "--use-local-bundle",
        str(cache / f"kani-{kani_version}-x86_64-unknown-linux-gnu.tar.gz"),
    ]
    assert records[1]["argv"] == expected_setup, (
        "setup must use the verified bundle and the configured Cargo"
    )


def test_verus_install_delegates_verus_and_z3(
    workdir: Path, fakes: FakeTools, tmp_path: Path
) -> None:
    """Verus and Z3 both install through the shared CLI with explicit paths."""
    verus_dir = tmp_path / "verus install dir"
    result = run_make(
        workdir,
        MakeCall("install-verus", assignments=(f"VERUS_INSTALL_DIR={verus_dir}",)),
        fakes,
    )
    assert result.returncode == 0, result.stderr

    records = fakes.records()
    assert [record["tool"] for record in records] == ["prover-tools", "prover-tools"], (
        "Verus installation must delegate both the verifier and the solver"
    )
    expected_verus = [
        "verus",
        "install",
        "--repo-root",
        str(workdir),
        "--install-dir",
        str(verus_dir),
    ]
    assert records[0]["argv"] == expected_verus, (
        "a spaced install path must reach the verifier as one argument"
    )
    z3_dir = workdir / ".cache/boundary-z3"
    expected_z3 = [
        "z3",
        "install",
        "--repo-root",
        str(workdir),
        "--install-dir",
        str(z3_dir),
        "--cache-dir",
        str(z3_dir),
    ]
    assert records[1]["argv"] == expected_z3, (
        "the solver must install into the repository cache as a local file"
    )


def test_verus_install_clears_the_z3_marker_first(
    workdir: Path, fakes: FakeTools
) -> None:
    """A stale install marker must not hide the newly verified solver archive."""
    marker = workdir / ".cache/boundary-z3/.z3-version"
    marker.parent.mkdir(parents=True)
    marker.write_text("4.16.0\n", encoding="utf-8")

    result = run_make(
        workdir,
        MakeCall("install-verus", environment={"FAKE_MARKER": str(marker)}),
        fakes,
    )
    assert result.returncode == 0, result.stderr

    records = fakes.records()
    assert records[0]["marker_exists"] is True, (
        "the verifier install must not disturb the existing solver cache"
    )
    assert records[1]["marker_exists"] is False, (
        "the marker must be cleared before the delegated solver install"
    )
    assert not marker.exists(), "the retired marker must not survive the reinstall"


def test_the_watched_environment_matches_the_makefile_guard() -> None:
    """The fake must watch every name the Makefile claims to clear."""
    assert set(ambient_guard_names()) == set(AMBIENT_INSTALL_VARIABLES), (
        "the ambient-name tuple must match PROVER_TOOLS_AMBIENT_VARS"
    )


@pytest.mark.parametrize("target", ["install-boundary-kani", "install-verus"])
def test_ambient_install_options_cannot_redirect_the_targets(
    workdir: Path, fakes: FakeTools, target: str
) -> None:
    """Stray CI variables must not override the explicit repository paths."""
    ambient = dict.fromkeys(AMBIENT_INSTALL_VARIABLES, "/ambient/wrong")
    result = run_make(workdir, MakeCall(target, environment=ambient), fakes)
    assert result.returncode == 0, result.stderr

    delegated = [
        record for record in fakes.records() if record["tool"] == "prover-tools"
    ]
    assert delegated, "the fake prover-tools was never invoked"
    leaked = [record["env"] for record in delegated if record["env"]]
    assert not leaked, f"ambient installer options reached the shared CLI: {leaked}"
    roots = [recorded_argv(record) for record in delegated]
    assert all(argv[argv.index("--repo-root") + 1] == str(workdir) for argv in roots), (
        f"{target} must resolve every delegated call against the checkout"
    )


def test_a_failed_z3_install_fails_the_make_target(
    workdir: Path, fakes: FakeTools
) -> None:
    """A delegated solver failure must stop the target and stay visible."""
    result = run_make(
        workdir,
        MakeCall(
            "install-verus",
            environment={"FAKE_FAIL_ARGS": json.dumps(["z3", "install"])},
        ),
        fakes,
    )
    assert result.returncode != 0, "a failed solver install must fail the target"
    assert "fake prover-tools failure" in result.stderr, (
        "the delegated failure must reach the caller's log"
    )
    assert [recorded_argv(record)[:2] for record in fakes.records()] == [
        ["verus", "install"],
        ["z3", "install"],
    ], "the verifier must install before the solver it drives"


def test_the_version_check_resolves_the_cached_kani_frontend(
    tmp_path: Path, fakes: FakeTools
) -> None:
    """A stale `cargo-kani` beside the local tools must not answer for the cache.

    The version check runs through the shared CLI, whose PATH would otherwise
    put `~/.local/bin` first, while the proof commands that follow run whatever
    `cargo kani` resolves from the target's own PATH. A frontend that differs
    between those two steps would verify a different Kani than the proof run.
    """
    workdir = tmp_path / "repo"
    cached_bin = workdir / ".cache/boundary-kani/bin"
    write_executable(cached_bin / "cargo-kani", FAKE_FRONTEND.format(identity="cached"))
    home = tmp_path / "home"
    stale = write_executable(
        home / ".local/bin/cargo-kani", FAKE_FRONTEND.format(identity="stale")
    )
    uv_dir = tmp_path / "uv-bin"
    write_executable(uv_dir / "uv", FAKE_UV)
    resolve_log = tmp_path / "resolve.log"
    frontend_log = tmp_path / "frontend.log"

    result = run_make(
        workdir,
        MakeCall(
            "boundary-kani",
            assignments=("RUST_DIR=.", "PROVER_TOOLS_SOURCE=test-source"),
            environment={
                "HOME": str(home),
                "PATH": f"{uv_dir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_RESOLVE_LOG": str(resolve_log),
                "FAKE_FRONTEND_LOG": str(frontend_log),
            },
        ),
        fakes,
        delegate=False,
    )
    assert result.returncode == 0, result.stderr

    recorded = recorded_values(resolve_log)
    assert recorded["resolved"] == str(cached_bin / "cargo-kani"), (
        "the version check must resolve the frontend the proof commands use; "
        f"its PATH was {recorded.get('path')!r}"
    )
    assert str(stale.parent) in recorded["path"].split(os.pathsep), (
        "the local tool path must still be searched, otherwise the ordering "
        "this case pins is not the one the recipe actually applies"
    )
    assert not frontend_log.exists(), (
        "no competing frontend may be executed by the version check"
    )


def test_the_retired_local_installers_are_gone() -> None:
    """A leftover entry point would reintroduce the unbounded disk write."""
    assert not (REPO_ROOT / "scripts/install_boundary_kani.py").exists(), (
        "the retired Kani installer must not return"
    )
    assert not (REPO_ROOT / "scripts/install_boundary_z3.py").exists(), (
        "the retired Z3 installer must not return"
    )
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "install_boundary_kani" not in text, (
        "the Makefile must not call the retired Kani installer"
    )
    assert "install_boundary_z3" not in text, (
        "the Makefile must not call the retired Z3 installer"
    )
