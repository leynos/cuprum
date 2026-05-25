"""Property-based tests for pure stream line-splitting helpers."""

from __future__ import annotations

import sys
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum._testing import _split_complete_lines, _strip_line_ending

_IS_PYTHON_315 = sys.version_info[:2] == (3, 15)

if _IS_PYTHON_315:
    AnalysisOptionSet = typ.cast("typ.Any", None)
    AnalysisKind = typ.cast("typ.Any", None)
    MessageType = typ.cast("typ.Any", None)
    check_states = typ.cast("typ.Any", None)
else:
    try:
        import crosshair.core_and_libs  # noqa: F401
        from crosshair.options import AnalysisKind, AnalysisOptionSet
        from crosshair.statespace import MessageType
        from crosshair.test_util import check_states
    except ImportError:  # pragma: no cover - exercised only without dev deps
        AnalysisOptionSet = typ.cast("typ.Any", None)
        AnalysisKind = typ.cast("typ.Any", None)
        MessageType = typ.cast("typ.Any", None)
        check_states = typ.cast("typ.Any", None)

_LINE_ENDINGS: tuple[str, str, str] = ("\r\n", "\n", "\r")
_PYTHON_LINE_BOUNDARIES: str = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_PROPERTY_SETTINGS: settings = settings(
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # Keep the issue-requested bound; CrossHair covers the symbolic edge cases.
    max_examples=30,
)


def _normalise_line_endings(text: str) -> str:
    """Normalize recognized line endings to line-feed characters."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _rebuild_normalised_text(lines: list[str], remainder: str) -> str:
    """Rebuild text from split output using normalized line endings."""
    return "".join(f"{line}\n" for line in lines) + remainder


def _line_ending_suffix(line: str) -> str:
    """Return the single trailing line-ending sequence, if present."""
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


def _split_preserves_normalised_text(text: str) -> bool:
    """Return whether split output accounts for all input text."""
    lines, remainder = _split_complete_lines(text)
    return len(_normalise_line_endings(text)) == (
        sum(len(line) + 1 for line in lines) + len(remainder)
    )


@st.composite
def _text_with_line_endings(draw: st.DrawFn) -> str:
    """Generate arbitrary text with embedded recognized line endings."""
    fragments = draw(
        st.lists(
            st.text(
                alphabet=st.characters(exclude_characters=_PYTHON_LINE_BOUNDARIES),
                max_size=8,
            ),
            min_size=1,
            max_size=8,
        ),
    )
    endings = draw(
        st.lists(
            st.sampled_from((*_LINE_ENDINGS, "")),
            min_size=len(fragments) - 1,
            max_size=len(fragments) - 1,
        ),
    )

    text = "".join(fragments[index] + ending for index, ending in enumerate(endings))
    return text + fragments[-1]


@st.composite
def _line_with_optional_ending(draw: st.DrawFn) -> str:
    """Generate one line that may have a recognized trailing line ending."""
    body = draw(
        st.text(alphabet=st.characters(exclude_characters=_PYTHON_LINE_BOUNDARIES)),
    )
    ending = draw(st.sampled_from((*_LINE_ENDINGS, "")))
    return body + ending


@_PROPERTY_SETTINGS
@given(text=_text_with_line_endings())
def test_split_complete_lines_preserves_all_text(text: str) -> None:
    """Property: splitting and rebuilding preserves normalized text.

    Parameters
    ----------
    text : str
        Generated text containing arbitrary recognised line endings.
    """
    lines, remainder = _split_complete_lines(text)

    assert _rebuild_normalised_text(lines, remainder) == _normalise_line_endings(
        text
    ), (
        "_split_complete_lines output rebuilt with _rebuild_normalised_text "
        "must match _normalise_line_endings input"
    )


@_PROPERTY_SETTINGS
@given(text=_text_with_line_endings())
def test_split_complete_lines_remainder_has_no_line_ending(text: str) -> None:
    """Property: the returned remainder is never a completed line.

    Parameters
    ----------
    text : str
        Generated text containing arbitrary recognised line endings.
    """
    _lines, remainder = _split_complete_lines(text)

    assert not remainder.endswith(("\n", "\r")), (
        "_split_complete_lines remainder must not end with a recognized line ending"
    )


@_PROPERTY_SETTINGS
@given(line=_line_with_optional_ending())
def test_strip_line_ending_idempotent(line: str) -> None:
    """Property: stripping a line ending is idempotent.

    Parameters
    ----------
    line : str
        Generated line with an optional recognised line ending.
    """
    stripped = _strip_line_ending(line)

    assert _strip_line_ending(stripped) == stripped, (
        "_strip_line_ending must be idempotent after the first strip"
    )


@_PROPERTY_SETTINGS
@given(line=_line_with_optional_ending())
def test_strip_line_ending_removes_only_trailing(line: str) -> None:
    """Property: stripping removes exactly one trailing line-ending sequence.

    Parameters
    ----------
    line : str
        Generated line with an optional recognised line ending.
    """
    suffix = _line_ending_suffix(line)

    assert _strip_line_ending(line) == line.removesuffix(suffix), (
        "_strip_line_ending must remove only the suffix reported by _line_ending_suffix"
    )


def _split_no_text_loss_contract(text: str) -> None:
    r"""CrossHair contract for split text preservation.

    pre: len(text) <= 3
    pre: all(character not in "\v\f\x1c\x1d\x1e\x85\u2028\u2029" for character in text)
    post: _split_preserves_normalised_text(text)
    """


def _strip_line_ending_contract(line: str) -> None:
    """CrossHair contract for one trailing line-ending removal.

    pre: len(line) <= 4
    post: _strip_line_ending(line) == line.removesuffix(_line_ending_suffix(line))
    """


@pytest.mark.skipif(check_states is None, reason="CrossHair is not installed")
def test_crosshair_split_no_text_loss() -> None:
    """Property: CrossHair symbolically verifies split text preservation."""
    check_states(
        _split_no_text_loss_contract,
        MessageType.CONFIRMED,
        AnalysisOptionSet(
            analysis_kind=(AnalysisKind.PEP316,),
            per_condition_timeout=10,
        ),
    )


@pytest.mark.skipif(check_states is None, reason="CrossHair is not installed")
def test_crosshair_strip_line_ending_contract() -> None:
    """Property: CrossHair symbolically verifies line-ending stripping."""
    check_states(
        _strip_line_ending_contract,
        MessageType.CONFIRMED,
        AnalysisOptionSet(
            analysis_kind=(AnalysisKind.PEP316,),
            per_condition_timeout=10,
        ),
    )
