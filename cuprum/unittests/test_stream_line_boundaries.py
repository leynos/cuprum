"""Regression tests for Python's complete decoded-line boundary set."""

from __future__ import annotations

import pytest

from cuprum._stream_line_boundaries import _split_complete_lines, _strip_line_ending

_BOUNDARIES = (
    "\r\n",
    "\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
)


@pytest.mark.parametrize("boundary", _BOUNDARIES)
def test_complete_python_line_boundary_is_emitted_without_its_separator(
    boundary: str,
) -> None:
    """Every ``str.splitlines()`` boundary completes one emitted line."""
    lines, remainder = _split_complete_lines(f"line{boundary}")

    assert lines == ["line"], f"{boundary!r} must complete and strip one line"
    assert remainder == "", f"{boundary!r} must leave no completed remainder"
    assert _strip_line_ending(f"line{boundary}") == "line", (
        f"{boundary!r} must be stripped from an emitted line"
    )


def test_non_final_carriage_return_remains_the_only_held_boundary() -> None:
    """Only a potentially split CRLF pair remains pending between chunks."""
    lines, remainder = _split_complete_lines("line\r", final=False)

    assert lines == [], "a non-final carriage return may still form CRLF"
    assert remainder == "line\r", "the potential CRLF pair must remain intact"
