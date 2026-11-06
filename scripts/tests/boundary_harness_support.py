"""Build isolated repository fixtures for the boundary harness tests.

The harnesses copy the real Rust workspace and run verifier commands through
the ``cuprum`` execution seam. Pointing them at a temporary root keeps their
orchestration exercised against production source text while confining every
write, and every recorded command, to the test.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Cargo metadata argv.
import typing as typ
from pathlib import Path

from scripts import check_boundary_contract
from scripts.boundary_workspace import copy_source_tree

if typ.TYPE_CHECKING:
    import pytest

# Cargo metadata may wait for the shared package-cache lock.
CARGO_METADATA_TIMEOUT_SECONDS = 120


def copy_boundary_repository(tmp_path: Path) -> Path:
    """Copy the Rust workspace and the tool pins into a temporary root.

    Returns
    -------
    Path
        Temporary root laid out like the repository, minus any build output.
    """
    copy_source_tree(check_boundary_contract.ROOT / "rust", tmp_path / "rust")
    shutil.copytree(check_boundary_contract.ROOT / "tools", tmp_path / "tools")
    return tmp_path


def latest_run_root(root: Path, harness: str) -> Path:
    """Return the single run directory a boundary harness retained.

    Parameters
    ----------
    root : Path
        Repository-like root the harness ran against.
    harness : str
        Harness name, which also names its ``.cache/<harness>/runs`` directory.

    Returns
    -------
    Path
        The one private run root the harness created.

    Raises
    ------
    RuntimeError
        If the harness retained no run root or more than one.
    """
    runs = sorted((root / f".cache/{harness}/runs").glob("run-*"))
    if len(runs) != 1:
        msg = f"the {harness} invocation must use one private run root"
        raise RuntimeError(msg)
    return runs[0]


def _no_preparation_time(_run_root: Path, _workspace: Path) -> float:
    """Report dependency preparation that consumed no measurable time."""
    return 0.0


def _no_fault_preparation_time(_root: Path, _run_root: Path, _workspace: Path) -> float:
    """Report a fault run's dependency preparation as consuming no time.

    The fault harness binds its repository root before handing the callable to
    the run context, so its preparation seam takes the bound root as well as
    the run's own paths.

    Returns
    -------
    float
        Zero, in place of a measured preparation duration.
    """
    return 0.0


def stub_cargo_preparation(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    """Make one harness skip Cargo-home preparation in its orchestration test.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Patch manager for the current test.
    module : object
        Production module exposing ``_prepare_run_cargo_home``.
    """
    monkeypatch.setattr(module, "_prepare_run_cargo_home", _no_preparation_time)


def recorded_phases(run_root: Path) -> list[str]:
    """Return the timing phases one harness recorded, in order.

    Parameters
    ----------
    run_root : Path
        Private run directory holding the harness's ``timings.jsonl``.

    Returns
    -------
    list of str
        Phase names in the order they were recorded.
    """
    lines = (run_root / "timings.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["phase"] for line in lines]


def cargo_target_roots(crate: Path) -> tuple[Path, ...]:
    """Return Cargo metadata source roots for the synthetic safe package."""
    cargo = shutil.which("cargo")
    if cargo is None:
        msg = "Cargo is required for the metadata contract"
        raise RuntimeError(msg)
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Cargo metadata argv runs in the isolated fixture.
        [cargo, "metadata", "--no-deps", "--format-version=1"],
        capture_output=True,
        check=False,
        cwd=crate,
        text=True,
        timeout=CARGO_METADATA_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)
    metadata = json.loads(completed.stdout)
    package = next(
        package
        for package in metadata["packages"]
        if package["name"] == "cuprum-streams"
    )
    return tuple(sorted(Path(target["src_path"]) for target in package["targets"]))


def record_cargo_metadata_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record Cargo metadata calls.

    Parameters
    ----------
    monkeypatch
        Pytest monkeypatch fixture. The patch remains active for the current
        test.

    Returns
    -------
    list[Path]
        Recorded crate paths.
    """
    calls: list[Path] = []
    read_metadata = cargo_target_roots

    def record(crate_path: Path) -> tuple[Path, ...]:
        """Count the call before delegating to Cargo metadata."""
        calls.append(crate_path)
        return read_metadata(crate_path)

    monkeypatch.setattr(
        "scripts.tests.boundary_harness_support.cargo_target_roots", record
    )
    return calls
