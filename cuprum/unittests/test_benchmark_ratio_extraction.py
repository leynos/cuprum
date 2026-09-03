"""Unit tests for extracting paired benchmark-ratchet scenario entries."""

from __future__ import annotations

import pytest

from benchmarks.ratchet_ratio_extraction import _extract_scenario_entry


def test_extract_scenario_entry_accepts_matching_command() -> None:
    """A result command may carry the logical name of its paired scenario."""
    entry = _extract_scenario_entry(
        index=0,
        scenario_value={"name": "rust-small", "backend": "rust"},
        result_value={"command": "rust-small", "mean": 1.5},
    )

    assert entry == ("small", "rust", 1.5), (
        "matching logical names must preserve comparison extraction"
    )


def test_extract_scenario_entry_rejects_mismatched_command() -> None:
    """A result command must identify the same scenario as the paired entry."""
    with pytest.raises(
        ValueError,
        match=(
            r"results\[0\]\.command 'python-small' must match "
            r"scenarios\[0\]\.name 'rust-small'"
        ),
    ):
        _extract_scenario_entry(
            index=0,
            scenario_value={"name": "rust-small", "backend": "rust"},
            result_value={"command": "python-small", "mean": 1.5},
        )


@pytest.mark.parametrize(
    ("result_value", "expected_exception"),
    [
        pytest.param({"mean": 1.5}, TypeError, id="missing"),
        pytest.param({"command": 7, "mean": 1.5}, TypeError, id="non-string"),
        pytest.param({"command": "", "mean": 1.5}, ValueError, id="empty"),
    ],
)
def test_extract_scenario_entry_rejects_invalid_command(
    result_value: dict[str, object], expected_exception: type[Exception]
) -> None:
    """Result commands must be present, string typed, and non-empty."""
    with pytest.raises(
        expected_exception,
        match=r"results\[0\]\.command must be a non-empty string",
    ):
        _extract_scenario_entry(
            index=0,
            scenario_value={"name": "rust-small", "backend": "rust"},
            result_value=result_value,
        )
