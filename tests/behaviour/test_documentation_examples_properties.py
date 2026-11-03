"""Property checks for the documentation gate's parsing contracts.

The parameterized suites name representative shapes and their diagnostics.
These properties sample the wider space the same code accepts, so a delimiter
length, indentation, or nesting combination the named cases never reach still
has to behave the way the contract claims.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.behaviour._documentation_examples_support import _parse_examples
from tests.behaviour.test_scoped_call_form_contract import (
    KEYWORD,
    _text_passes_retired_keyword,
)

FENCE_CHARS = st.sampled_from(["`", "~"])
DELIMITER_LENGTHS = st.integers(min_value=3, max_value=6)
INDENTS = st.integers(min_value=0, max_value=3)
PLAIN_TEXT = st.text(alphabet=st.sampled_from("abcXYZ 0123456789"), max_size=20)


@st.composite
def _closing_runs(draw: st.DrawFn) -> tuple[str, str]:
    """Draw an opener and a closer at least as long as it."""
    char = draw(FENCE_CHARS)
    opening = draw(DELIMITER_LENGTHS)
    closing = opening + draw(st.integers(min_value=0, max_value=3))
    return char * opening, char * closing


@st.composite
def _short_closers(draw: st.DrawFn) -> tuple[str, str]:
    """Draw an opener and a shorter closer of the same character."""
    char = draw(FENCE_CHARS)
    opening = draw(st.integers(min_value=4, max_value=6))
    closing = opening - draw(st.integers(min_value=1, max_value=3))
    return char * opening, char * closing


@st.composite
def _mixed_markers(draw: st.DrawFn) -> tuple[str, str]:
    """Draw an opener and an equal-length closer using the other character."""
    char = draw(FENCE_CHARS)
    other = "~" if char == "`" else "`"
    length = draw(DELIMITER_LENGTHS)
    return char * length, other * length


def _marked_document(
    runs: tuple[str, str], indent: int, comment: str
) -> tuple[str, str]:
    """Return a marked fence document and the code its body should yield."""
    code = f"value = 1  # {comment}\nassert value == 1\n"
    prefix = " " * indent
    body = "".join(f"{prefix}{line}\n" for line in code.splitlines())
    text = (
        f"{prefix}<!-- tested-example: sample -->\n"
        f"{prefix}{runs[0]}python\n"
        f"{body}"
        f"{prefix}{runs[1]}\n"
    )
    return text, code


@given(runs=_closing_runs(), indent=INDENTS, comment=PLAIN_TEXT)
def test_a_valid_tested_fence_round_trips_its_body(
    runs: tuple[str, str], indent: int, comment: str
) -> None:
    """Any valid delimiter run and container indent yields its body unchanged."""
    text, code = _marked_document(runs, indent, comment)
    assert _parse_examples("doc.md", text) == [("sample", code)], (
        f"{text!r} did not round-trip"
    )


@given(runs=_short_closers(), indent=INDENTS, comment=PLAIN_TEXT)
def test_a_closer_shorter_than_its_opener_leaves_the_fence_open(
    runs: tuple[str, str], indent: int, comment: str
) -> None:
    """A closing run below the opener's length is body content, not a closer."""
    text, _ = _marked_document(runs, indent, comment)
    with pytest.raises(AssertionError, match="unterminated fence"):
        _parse_examples("doc.md", text)


@given(runs=_mixed_markers(), indent=INDENTS, comment=PLAIN_TEXT)
def test_the_other_marker_character_never_closes_a_fence(
    runs: tuple[str, str], indent: int, comment: str
) -> None:
    """Backticks and tildes are distinct delimiters even at equal length."""
    text, _ = _marked_document(runs, indent, comment)
    with pytest.raises(AssertionError, match="unterminated fence"):
        _parse_examples("doc.md", text)


@given(filler=PLAIN_TEXT)
def test_a_direct_keyword_is_detected_among_sampled_arguments(filler: str) -> None:
    """The text scan finds the retired keyword however arguments are spaced."""
    text = f"scoped(catalogue=CATALOGUE, {filler} {KEYWORD}=mate.allowlist)"
    assert _text_passes_retired_keyword(text), text


@given(filler=PLAIN_TEXT)
def test_a_keyword_one_call_deep_is_not_a_direct_argument(filler: str) -> None:
    """A keyword inside a nested call belongs to that call, not to ``scoped``."""
    text = f"scoped(catalogue=build({filler} {KEYWORD}=mate.allowlist))"
    assert not _text_passes_retired_keyword(text), text
