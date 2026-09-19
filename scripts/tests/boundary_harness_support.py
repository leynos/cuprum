"""Build isolated repository fixtures for the boundary harness tests.

The harnesses copy the real Rust workspace and run verifier commands through
the ``cuprum`` execution seam. Pointing them at a temporary root keeps their
orchestration exercised against production source text while confining every
write, and every recorded command, to the test.
"""

from __future__ import annotations

import shutil
import typing as typ

from scripts import check_boundary_contract

if typ.TYPE_CHECKING:
    from pathlib import Path

# Build output is the only part of the workspace a harness must never copy.
IGNORED = shutil.ignore_patterns("target")


def copy_boundary_repository(tmp_path: Path) -> Path:
    """Copy the Rust workspace and the tool pins into a temporary root.

    Returns
    -------
    Path
        Temporary root laid out like the repository, minus any build output.
    """
    shutil.copytree(
        check_boundary_contract.ROOT / "rust", tmp_path / "rust", ignore=IGNORED
    )
    shutil.copytree(check_boundary_contract.ROOT / "tools", tmp_path / "tools")
    return tmp_path
