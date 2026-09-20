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
from pathlib import Path

from scripts import check_boundary_contract
from scripts.boundary_workspace import copy_source_tree


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
