"""Property checks for the documentation gate's parsing contracts.

The parameterized suites name representative shapes and their diagnostics.
These properties sample the wider space the same code accepts, so a delimiter
length, indentation, or nesting combination the named cases never reach still
has to behave the way the contract claims. Each property compares the gate with
an independent model of the rule it enforces rather than with a value the gate
itself produced.
"""

from __future__ import annotations

import dataclasses as dc

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.behaviour._documentation_examples_support import _parse_examples
from tests.behaviour._retired_call_scan import KEYWORD, _text_passes_retired_keyword

FENCE_CHARS = st.sampled_from(["`", "~"])
DELIMITER_LENGTHS = st.integers(min_value=3, max_value=6)
CONTAINER_INDENTS = st.integers(min_value=0, max_value=5)
PLAIN_TEXT = st.text(alphabet=st.sampled_from("abcXYZ 0123456789"), max_size=20)
PAYLOAD = st.text(alphabet=st.sampled_from("abcXYZ 0123456789()[]{}'=,"), max_size=12)


@dc.dataclass(frozen=True, slots=True)
class _FenceLayout:
    """A drawn fence: its two delimiter runs and three indentation widths."""

    opening: str
    closing: str
    container: int
    body: int
    closer: int


@dc.dataclass(frozen=True, slots=True)
class _Call:
    """A drawn ``scoped()`` call, its surrounding text, and its verdict."""

    text: str
    is_retired: bool


@st.composite
def _valid_layouts(draw: st.DrawFn) -> _FenceLayout:
    """Draw a fence whose closer is a valid delimiter for its opener."""
    marker = draw(FENCE_CHARS)
    opening = draw(DELIMITER_LENGTHS)
    container = draw(CONTAINER_INDENTS)
    return _FenceLayout(
        opening=marker * opening,
        closing=marker * (opening + draw(st.integers(min_value=0, max_value=3))),
        container=container,
        body=draw(st.integers(min_value=max(0, container - 3), max_value=container)),
        closer=draw(st.integers(min_value=0, max_value=container + 3)),
    )


@st.composite
def _short_closers(draw: st.DrawFn) -> _FenceLayout:
    """Draw a fence whose closer is shorter than its opener."""
    marker = draw(FENCE_CHARS)
    opening = draw(st.integers(min_value=4, max_value=6))
    container = draw(CONTAINER_INDENTS)
    return _FenceLayout(
        opening=marker * opening,
        closing=marker * (opening - draw(st.integers(min_value=1, max_value=3))),
        container=container,
        body=draw(st.integers(min_value=0, max_value=8)),
        closer=draw(st.integers(min_value=0, max_value=container + 3)),
    )


@st.composite
def _mixed_markers(draw: st.DrawFn) -> _FenceLayout:
    """Draw a fence whose closer uses the other marker character."""
    marker = draw(FENCE_CHARS)
    other = "~" if marker == "`" else "`"
    length = draw(DELIMITER_LENGTHS)
    container = draw(CONTAINER_INDENTS)
    return _FenceLayout(
        opening=marker * length,
        closing=other * length,
        container=container,
        body=draw(st.integers(min_value=0, max_value=8)),
        closer=draw(st.integers(min_value=0, max_value=container + 3)),
    )


@st.composite
def _over_indented_closers(draw: st.DrawFn) -> _FenceLayout:
    """Draw a fence whose closer sits more than three columns past its container."""
    marker = draw(FENCE_CHARS)
    opening = draw(DELIMITER_LENGTHS)
    container = draw(CONTAINER_INDENTS)
    return _FenceLayout(
        opening=marker * opening,
        closing=marker * (opening + draw(st.integers(min_value=0, max_value=3))),
        container=container,
        body=draw(st.integers(min_value=0, max_value=8)),
        closer=container + draw(st.integers(min_value=4, max_value=7)),
    )


def _document(layout: _FenceLayout, comment: str) -> str:
    """Return a marked fence document for ``layout``."""
    container = " " * layout.container
    body = (
        f"{' ' * layout.body}if True:\n"
        f"{' ' * (layout.body + 4)}assert value == 1  # {comment}\n"
    )
    return (
        f"{container}<!-- tested-example: sample -->\n"
        f"{container}{layout.opening}python\n"
        f"{body}"
        f"{' ' * layout.closer}{layout.closing}\n"
    )


def _expected_code(layout: _FenceLayout, comment: str) -> str:
    """Return the code CommonMark dedenting must yield for ``layout``."""
    first = " " * max(layout.body - layout.container, 0)
    second = " " * max(layout.body + 4 - layout.container, 0)
    return f"{first}if True:\n{second}assert value == 1  # {comment}\n"


@given(layout=_valid_layouts(), comment=PLAIN_TEXT)
def test_a_valid_tested_fence_round_trips_its_dedented_body(
    layout: _FenceLayout, comment: str
) -> None:
    """A valid delimiter and indentation set yields the body CommonMark dedents."""
    text = _document(layout, comment)
    expected = [("sample", _expected_code(layout, comment))]
    assert _parse_examples("doc.md", text) == expected, f"{text!r} did not round-trip"


@given(layout=_short_closers(), comment=PLAIN_TEXT)
def test_a_closer_shorter_than_its_opener_leaves_the_fence_open(
    layout: _FenceLayout, comment: str
) -> None:
    """A closing run below the opener's length is body content, not a closer."""
    text = _document(layout, comment)
    with pytest.raises(AssertionError, match="unterminated fence"):
        _parse_examples("doc.md", text)


@given(layout=_mixed_markers(), comment=PLAIN_TEXT)
def test_the_other_marker_character_never_closes_a_fence(
    layout: _FenceLayout, comment: str
) -> None:
    """Backticks and tildes are distinct delimiters even at equal length."""
    text = _document(layout, comment)
    with pytest.raises(AssertionError, match="unterminated fence"):
        _parse_examples("doc.md", text)


@given(layout=_over_indented_closers(), comment=PLAIN_TEXT)
def test_a_closer_beyond_three_columns_never_closes_a_fence(
    layout: _FenceLayout, comment: str
) -> None:
    """A closer indented past its container's three-column allowance is content."""
    text = _document(layout, comment)
    with pytest.raises(AssertionError, match="unterminated fence"):
        _parse_examples("doc.md", text)


def _single_quoted(text: str) -> str:
    """Return ``text`` as a single-quoted Python literal."""
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


ATOMS = st.one_of(
    st.sampled_from(["CATALOGUE", "None", "42", "mate.allowlist"]),
    st.builds(_single_quoted, PAYLOAD),
    st.sampled_from(["'allowlist=mate.allowlist'", "'scoped(allowlist=m)'"]),
)


@st.composite
def _group_values(draw: st.DrawFn) -> str:
    """Draw a bracketed group that may carry a nested keyword."""
    opener, closer = draw(st.sampled_from([("build(", ")"), ("[", "]"), ("{", "}")]))
    items = draw(st.lists(ATOMS, max_size=3))
    if draw(st.booleans()):
        items.append(f"{KEYWORD}=mate.allowlist")
    return f"{opener}{', '.join(items)}{closer}"


def _embedded(call: str, container: str) -> str:
    """Return ``call`` placed in ``container`` so the scan must classify it."""
    match container:
        case "bare":
            return f"usage: {call}"
        case "fstring":
            return f'usage: f"{{{call}}}"'
        case _:
            return f'usage: "{call}"'


@st.composite
def _calls(draw: st.DrawFn) -> _Call:
    """Draw a call in surrounding text and whether the scan must report it."""
    arguments = [f"catalogue={draw(ATOMS)}"]
    if draw(st.booleans()):
        arguments.insert(
            draw(st.integers(min_value=0, max_value=len(arguments))),
            draw(_group_values()),
        )
    is_retired = draw(st.booleans())
    if is_retired:
        arguments.insert(
            draw(st.integers(min_value=0, max_value=len(arguments))),
            f"{KEYWORD}=mate.allowlist",
        )
    container = draw(st.sampled_from(["bare", "fstring", "string"]))
    return _Call(
        text=_embedded(f"scoped({', '.join(arguments)})", container),
        is_retired=is_retired and container != "string",
    )


@given(call=_calls())
def test_the_text_scan_classifies_generated_calls(call: _Call) -> None:
    """A direct keyword is reported; strings and nested calls are not."""
    detected = _text_passes_retired_keyword(call.text)
    assert detected is call.is_retired, f"{call.text!r}: detected {detected}"
