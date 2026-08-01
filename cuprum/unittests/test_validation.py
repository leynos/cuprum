"""Tests for the shared benchmark JSON-shape validators."""

from __future__ import annotations

import pytest

from benchmarks._validation import _require_non_empty_string


@pytest.mark.parametrize(
    "value",
    [None, 123, 1.5, b"bytes", ["a"], object()],
    ids=["none", "int", "float", "bytes", "list", "object"],
)
def test_require_non_empty_string_rejects_non_str(value: object) -> None:
    """Non-string inputs raise ``TypeError`` rather than ``ValueError``."""
    with pytest.raises(TypeError, match="field must be a non-empty string"):
        _require_non_empty_string(value, name="field")


@pytest.mark.parametrize(
    "value",
    ["", " ", "\t\n", "   "],
    ids=["empty", "space", "whitespace", "spaces"],
)
def test_require_non_empty_string_rejects_blank_str(value: str) -> None:
    """A string that is empty once stripped raises ``ValueError``."""
    with pytest.raises(ValueError, match="field must be a non-empty string"):
        _require_non_empty_string(value, name="field")


@pytest.mark.parametrize(
    "value",
    ["x", "  padded  ", "name"],
    ids=["single", "padded", "word"],
)
def test_require_non_empty_string_returns_value_unchanged(value: str) -> None:
    """A non-empty string is returned unchanged, retaining surrounding space."""
    assert _require_non_empty_string(value, name="field") == value, (
        "valid input must be returned unchanged"
    )
