"""Property tests for the doctest scanner's lexer, fence parser and import reader.

The example tables in ``test_rust_doctest_support`` pin known cases. The
scanner's invariants hold over unbounded text, though: comments nest, string
literals can carry any delimiter, fences vary in character and length, and an
import can be written in many equivalent ways. These tests generate that input.

Each generator builds the text and its expected result from the same
independent pieces, so the oracle never calls the code under test. A segment
knows what it contributes to ``strip_comments`` and to ``executable_code``; a
fenced block knows its attributes and body; an import group knows the names it
imports however it is written.
"""

from __future__ import annotations

import itertools
import typing as typ

from hypothesis import given
from hypothesis import strategies as st

from cuprum.unittests._rust_doctest_support import (
    Fence,
    code_blocks,
    compile_fail_count,
    doc_comment_groups,
    executable_code,
    imported_names,
    strip_comments,
)

_PRINTABLE = st.characters(min_codepoint=32, max_codepoint=126)


class _Segment(typ.NamedTuple):
    """One piece of Rust source, with what each scanner view must make of it."""

    text: str
    stripped: str
    executable: str


# Code avoids `/`, quotes and `#` so it cannot open a comment or a literal, and
# avoids `r` so it cannot start a raw string; adjacent segments then cannot
# combine into a different token.
_CODE = st.text(
    alphabet="abcdefghijklmnopqstuvwxyz0123456789 ;:{},()\n", max_size=12
).map(lambda text: _Segment(text, text, text))

# A line comment may hold anything, including quotes and block-comment markers.
_LINE_COMMENT = st.text(alphabet=_PRINTABLE, max_size=15).map(
    lambda body: _Segment(f"//{body}\n", "\n", "\n")
)

# Block comments nest. Their plain text avoids `/` and `*` so that it cannot
# form a marker with its neighbours, but may hold quotes and `//`-free text.
_BLOCK_PLAIN = st.text(
    alphabet=st.characters(
        min_codepoint=32, max_codepoint=126, exclude_characters="/*"
    ),
    max_size=6,
)
_BLOCK_TEXT = st.recursive(
    _BLOCK_PLAIN.map(lambda plain: f"/*{plain}*/"),
    lambda inner: st.lists(st.one_of(_BLOCK_PLAIN, inner), max_size=3).map(
        lambda parts: "/*" + "".join(parts) + "*/"
    ),
    max_leaves=6,
)
_BLOCK_COMMENT = _BLOCK_TEXT.map(lambda text: _Segment(text, "", ""))

# A string holds any delimiter and escapes, but no bare quote or backslash.
_STRING_PART = st.one_of(
    st.characters(min_codepoint=32, max_codepoint=126, exclude_characters='"\\'),
    st.sampled_from(['\\"', "\\\\", "\\n"]),
)
_STRING = st.lists(_STRING_PART, max_size=8).map(
    lambda parts: _Segment(f'"{"".join(parts)}"', f'"{"".join(parts)}"', '""')
)


@st.composite
def _raw_string(draw: st.DrawFn) -> _Segment:
    """Draw a raw string of any hash count, whose body holds no quote."""
    hashes = "#" * draw(st.integers(min_value=0, max_value=3))
    body = draw(
        st.text(
            alphabet=st.characters(
                min_codepoint=32, max_codepoint=126, exclude_characters='"'
            ),
            max_size=8,
        )
    )
    literal = f'r{hashes}"{body}"{hashes}'
    return _Segment(literal, literal, '""')


_SOURCES = st.lists(
    st.one_of(_CODE, _LINE_COMMENT, _BLOCK_COMMENT, _STRING, _raw_string()),
    max_size=8,
)


@given(_SOURCES)
def test_comment_stripping_removes_exactly_the_comments(
    segments: list[_Segment],
) -> None:
    """Comments vanish, and strings and code survive untouched, at any nesting."""
    text = "".join(segment.text for segment in segments)
    expected = "".join(segment.stripped for segment in segments)
    assert strip_comments(text) == expected, (
        "only comments may be removed, whatever delimiters strings and nested "
        "comments contain"
    )


@given(_SOURCES)
def test_executable_code_is_code_with_comments_removed_and_strings_emptied(
    segments: list[_Segment],
) -> None:
    """A string's contents never reach the import reader, however they look."""
    text = "".join(segment.text for segment in segments)
    expected = "".join(segment.executable for segment in segments)
    assert executable_code(text) == expected, (
        "comments must be removed and every string literal replaced by an empty one"
    )


@given(_SOURCES)
def test_comment_stripping_is_idempotent(segments: list[_Segment]) -> None:
    """Stripping leaves nothing further to strip."""
    once = strip_comments("".join(segment.text for segment in segments))
    assert strip_comments(once) == once, "a second pass must remove nothing"


# --- fences ---------------------------------------------------------------

_ATTRIBUTES = st.sampled_from([
    "compile_fail",
    "E0308",
    "E0603",
    "rust",
    "no_run",
    "ignore",
    "edition2024",
])
_SEPARATORS = st.sampled_from([",", " ", ", ", " ,", "\t", " , "])
# Prose never starts a fence, so it cannot close or open one by accident.
_PROSE = st.text(
    alphabet=st.characters(
        min_codepoint=32, max_codepoint=126, exclude_characters="`~"
    ),
    max_size=20,
)


class _Block(typ.NamedTuple):
    """A fenced block as written, with the attributes and body it must yield."""

    source: str
    attributes: tuple[str, ...]
    body: tuple[str, ...]


@st.composite
def _fenced_block(draw: st.DrawFn) -> _Block:
    """Draw a fenced block whose body holds fence-like lines that must not close it."""
    marker = draw(st.sampled_from("`~"))
    opening = draw(st.integers(min_value=3, max_value=6))
    closing = draw(st.integers(min_value=opening, max_value=8))
    attributes = tuple(draw(st.lists(_ATTRIBUTES, max_size=3)))
    separators = draw(st.lists(_SEPARATORS, min_size=3, max_size=3))
    info = "".join(
        name + separators[index % len(separators)]
        for index, name in enumerate(attributes[:-1])
    ) + "".join(attributes[-1:])
    other = "~" if marker == "`" else "`"
    fence_like = [
        st.integers(min_value=3, max_value=8).map(lambda size: other * size),
        st.integers(min_value=3, max_value=8).map(lambda size: marker * size + "rust"),
    ]
    if opening > 3:
        fence_like.append(
            st.integers(min_value=3, max_value=opening - 1).map(
                lambda size: marker * size
            )
        )
    lines = draw(st.lists(st.one_of(_PROSE, *fence_like), max_size=5))
    written = [f"/// {marker * opening}{info}"]
    written += [f"/// {line}" for line in lines]
    written += [f"/// {marker * closing}", "fn item() {}"]
    return _Block(
        "\n".join(written) + "\n", attributes, tuple(f" {line}" for line in lines)
    )


@given(_fenced_block())
def test_a_fence_closes_only_on_a_bare_fence_at_least_as_long(block: _Block) -> None:
    """Shorter, labelled or other-character fences are content, not closings."""
    (group,) = doc_comment_groups(block.source)
    assert code_blocks(group) == [Fence(1, block.attributes, block.body)], (
        "the block must keep its attributes, split on commas and whitespace, and "
        "every body line, including fence-like ones"
    )


@given(_fenced_block())
def test_a_block_counts_as_compile_fail_exactly_when_it_says_so(block: _Block) -> None:
    """The inventory sees a compile-fail block whatever its fence or separators."""
    expected = int("compile_fail" in block.attributes)
    assert compile_fail_count(block.source) == expected, (
        "a block is compile-fail exactly when `compile_fail` is among its attributes"
    )


# --- imports --------------------------------------------------------------

_IDENT = st.from_regex(r"[a-z][a-z0-9_]{0,5}", fullmatch=True).filter(
    lambda name: name != "use"
)
_WHITESPACE = st.text(alphabet=" \t\n", max_size=3)
_SOME_WHITESPACE = st.text(alphabet=" \t\n", min_size=1, max_size=3)
# Noise that looks like an import but is a comment or string, so is not one.
_NOISE = st.sampled_from([
    "// use fake::line;",
    "/* use fake::block; */",
    'let _s = "use fake::string;";',
    'let _r = r#"use fake::raw;"#;',
    "/* outer /* inner */ use fake::nested; */",
])


class _Layout(typ.NamedTuple):
    """How one import is written; none of it may change the names it imports."""

    is_braced: bool
    is_hidden: bool
    separator: str
    gaps: tuple[str, ...]


_LAYOUT = st.builds(
    _Layout,
    st.booleans(),
    st.booleans(),
    _SOME_WHITESPACE,
    st.lists(_WHITESPACE, min_size=5, max_size=5).map(tuple),
)


def _hide(statement: str) -> str:
    """Mark every line of a statement as a hidden doctest line."""
    return "\n".join(f"# {line}" for line in statement.split("\n"))


def _render_import(path: str, names: list[str], layout: _Layout) -> str:
    """Write ``names`` under ``path`` as the layout says, and nothing else."""
    if layout.is_braced:
        gaps = itertools.cycle(layout.gaps)
        inner = ",".join(f"{next(gaps)}{name}" for name in names) + next(gaps)
        statements = [f"use{layout.separator}{path}::{{{inner}}};"]
    else:
        statements = [f"use{layout.separator}{path}::{name};" for name in names]
    if layout.is_hidden:
        statements = [_hide(statement) for statement in statements]
    return "\n".join(statements)


@st.composite
def _import_group(draw: st.DrawFn) -> tuple[str, frozenset[str]]:
    """Draw one import written one of several equivalent ways, and its names."""
    path = "::".join(draw(st.lists(_IDENT, min_size=1, max_size=3)))
    names = draw(st.lists(_IDENT, min_size=1, max_size=4, unique=True))
    expected = frozenset(f"{path}::{name}" for name in names)
    return _render_import(path, names, draw(_LAYOUT)), expected


_IMPORT_ITEMS = st.lists(
    st.one_of(_import_group(), _NOISE.map(lambda noise: (noise, frozenset[str]()))),
    max_size=6,
)


@given(_IMPORT_ITEMS)
def test_imports_read_the_same_however_they_are_written(
    items: list[tuple[str, frozenset[str]]],
) -> None:
    """Braces, whitespace, hidden lines and noise never change the names read."""
    body = tuple(line for text, _ in items for line in text.split("\n"))
    expected = frozenset().union(*(names for _, names in items))
    assert imported_names(Fence(1, (), body)) == expected, (
        "an import is read from executable code only, and equivalent spellings "
        "give the same set of full paths"
    )
