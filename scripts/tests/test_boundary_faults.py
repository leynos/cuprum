"""Fault injection fails closed when the production correspondence changes."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_boundary_faults as faults
from scripts.check_boundary_faults import mutate

ROOT = Path(__file__).resolve().parents[2]


def test_the_harness_launches_the_pinned_installer_versions() -> None:
    """A drifted pin must fail here rather than at verifier launch."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert f"KANI_VERSION = {faults.KANI_VERSION}" in makefile, (
        "the harness Kani pin drifted from the Makefile's installer"
    )
    assert f"cuprum-verus-{faults.VERUS_VERSION}" in makefile, (
        "the harness Verus root drifted from the Makefile's installer"
    )


@pytest.mark.parametrize("source", ["absent", "old old"])
def test_fault_rejects_missing_or_ambiguous_kernel(source: str) -> None:
    """A stale mutation must fail rather than claiming harness sensitivity."""
    with pytest.raises(ValueError, match="exactly one"):
        mutate(source, "old", "new")


def test_fault_changes_only_the_named_fragment() -> None:
    """Surrounding executable code remains the production implementation."""
    assert mutate("before old after", "old", "new") == "before new after", (
        "fault changed surrounding production code"
    )
