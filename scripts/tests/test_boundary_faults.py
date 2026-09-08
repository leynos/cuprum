"""Fault injection fails closed when the production correspondence changes."""

import pytest

from scripts.check_boundary_faults import mutate


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
