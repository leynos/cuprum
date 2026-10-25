"""Scanner for the Rust doctests that pin compile-time contracts.

A `compile_fail` doctest passes whenever its snippet fails to compile. Stable
rustdoc, including the pinned 1.85.0 that CI uses, accepts the error code in
``compile_fail,E0308`` without checking it; only nightly rustdoc, which the
dev-fast route of `make test` uses, enforces it. On the other toolchains a
negative case passes just as readily for an unresolved import, so the positive
control in the same doc comment is what keeps it honest. This module reads
doc comments and reports a negative case that a control does not back: one
without an error code or a control, or one that imports a name no control
imports, because a rename of that name would then fail only the negative case,
and fail it the wrong way.

See ``docs/adr-027-compile-contracts-in-the-build-graph.md``.
"""

from __future__ import annotations

import re
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_OPENING_FENCE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_CLOSING_FENCE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*$")
_ATTRIBUTE_SEPARATOR = re.compile(r"[,\s]+")
_ERROR_CODE = re.compile(r"^E\d{4}$")
_RAW_STRING_START = re.compile(r'r(?P<hashes>#*)"')
_USE_STATEMENT = re.compile(r"\buse\s+(?P<spec>[^;]+);")
_NON_COMPILING = frozenset({"ignore", "compile_fail", "text", "console", "sh"})


class _Opening(typ.NamedTuple):
    """An opening code fence, with what a matching closing fence must repeat."""

    line: int
    marker: str
    attributes: tuple[str, ...]


class Fence(typ.NamedTuple):
    """One code block inside a doc comment."""

    line: int
    attributes: tuple[str, ...]
    body: tuple[str, ...]


def doc_comment_groups(source: str) -> cabc.Iterator[list[tuple[int, str]]]:
    """Yield each run of consecutive same-style doc comment lines.

    A run ends at any other line or a change of marker, which is where rustdoc
    ends one item's documentation.

    Yields
    ------
    list[tuple[int, str]]
        One run's lines, each as its one-based line number and the text after
        the ``///`` or ``//!`` marker.
    """
    group: list[tuple[int, str]] = []
    marker = ""
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.lstrip()
        current = stripped[:3] if stripped.startswith(("///", "//!")) else ""
        if current != marker and group:
            yield group
            group = []
        marker = current
        if current:
            group.append((number, stripped[3:]))
    if group:
        yield group


def _opening_fence(number: int, text: str) -> _Opening | None:
    """Read an opening fence, or return ``None`` when the line is not one."""
    match = _OPENING_FENCE.match(text)
    if match is None:
        return None
    marker, info = match["fence"], match["info"].strip()
    if marker[0] == "`" and "`" in info:
        return None
    attributes = tuple(part for part in _ATTRIBUTE_SEPARATOR.split(info) if part)
    return _Opening(number, marker, attributes)


def _closes(text: str, opening: _Opening) -> bool:
    """Report whether a line closes the block ``opening`` started.

    Returns
    -------
    bool
        ``True`` for a bare fence of the same character that is at least as
        long as the opening one. A line with an info string, such as
        ```` ```rust ````, is block content, as CommonMark specifies.
    """
    match = _CLOSING_FENCE.match(text)
    if match is None:
        return False
    fence = match["fence"]
    return fence[0] == opening.marker[0] and len(fence) >= len(opening.marker)


def code_blocks(group: list[tuple[int, str]]) -> list[Fence]:
    """Return the code blocks in a doc comment, with their attributes and text."""
    blocks: list[Fence] = []
    opening: _Opening | None = None
    body: list[str] = []
    for number, text in group:
        if opening is None:
            opening, body = _opening_fence(number, text), []
        elif _closes(text, opening):
            blocks.append(Fence(opening.line, opening.attributes, tuple(body)))
            opening = None
        else:
            body.append(text)
    return blocks


def is_compile_fail(fence: Fence) -> bool:
    """Report whether a block declares a compile-fail doctest."""
    return "compile_fail" in fence.attributes


def is_positive_control(fence: Fence) -> bool:
    """Report whether a block declares a Rust doctest that must compile."""
    return not _NON_COMPILING.intersection(fence.attributes) and all(
        attribute in {"rust", "no_run", "should_panic"}
        or attribute.startswith("edition")
        for attribute in fence.attributes
    )


def _block_comment_end(text: str, start: int) -> int:
    """Return the index after the (possibly nested) block comment at ``start``."""
    depth = 0
    index = start
    while index < len(text):
        if text.startswith("/*", index):
            depth, index = depth + 1, index + 2
        elif text.startswith("*/", index):
            depth, index = depth - 1, index + 2
            if depth == 0:
                return index
        else:
            index += 1
    return len(text)


def _string_end(text: str, start: int) -> int:
    """Return the index after the string literal whose opening quote is ``start``."""
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == '"':
            return index + 1
        else:
            index += 1
    return len(text)


def _raw_string_end(text: str, raw_start: re.Match[str]) -> int:
    """Return the index after the raw string literal that ``raw_start`` opens."""
    closing = '"' + raw_start["hashes"]
    found = text.find(closing, raw_start.end())
    return len(text) if found == -1 else found + len(closing)


def _next_token(text: str, index: int) -> tuple[int, bool]:
    """Return where the token at ``index`` ends, and whether the code keeps it."""
    if text.startswith("//", index):
        end = text.find("\n", index)
        return (len(text) if end == -1 else end), False
    if text.startswith("/*", index):
        return _block_comment_end(text, index), False
    if text[index] == '"':
        return _string_end(text, index), True
    if raw_start := _RAW_STRING_START.match(text, index):
        return _raw_string_end(text, raw_start), True
    if text.startswith("'\"'", index):
        return index + 3, True
    return index + 1, True


def strip_comments(text: str) -> str:
    """Remove Rust comments from ``text``, leaving string literals intact.

    Block comments nest, as they do in Rust, and a comment marker inside a
    string, raw string or the ``'"'`` character literal is not a comment. An
    unterminated block comment runs to the end of the text. Lifetimes and other
    character literals are not interpreted, which is enough for the import
    lines this scanner reads.

    Returns
    -------
    str
        ``text`` without its comments; the newline that ends a line comment is
        kept, so line structure is preserved.
    """
    parts: list[str] = []
    index = 0
    while index < len(text):
        end, is_kept = _next_token(text, index)
        if is_kept:
            parts.append(text[index:end])
        index = end
    return "".join(parts)


def imported_names(fence: Fence) -> frozenset[str]:
    """Return every name a block imports, as whitespace-free full paths.

    Hidden doctest lines (a leading ``# ``) count, and a braced import list
    expands to one path per item, so a statement that rustfmt wrapped over
    several lines reads the same as a one-line one. Rust comments are removed
    first, so a commented-out import does not count as one.

    Returns
    -------
    frozenset[str]
        One full path per imported name, such as ``a::b::c``.
    """
    text = "\n".join(line.removeprefix("# ") for line in fence.body)
    text = strip_comments(text)
    names: set[str] = set()
    for statement in _USE_STATEMENT.finditer(text):
        spec = re.sub(r"\s+", "", statement["spec"])
        prefix, brace, rest = spec.partition("{")
        if brace:
            names.update(
                f"{prefix}{item}" for item in rest.rstrip("}").split(",") if item
            )
        else:
            names.add(spec)
    return frozenset(names)


def _block_defects(fence: Fence, controls: list[Fence]) -> list[str]:
    """Return each way one negative case is not backed by its controls."""
    defects: list[str] = []
    if not any(_ERROR_CODE.match(part) for part in fence.attributes):
        defects.append(f"line {fence.line}: names no expected error code")
    if not controls:
        defects.append(f"line {fence.line}: has no positive control")
        return defects
    unbacked = imported_names(fence).difference(*map(imported_names, controls))
    if unbacked:
        defects.append(
            f"line {fence.line}: imports {sorted(unbacked)} that no positive "
            f"control in the same doc comment imports"
        )
    return defects


def compile_fail_defects(source: str) -> list[str]:
    """Return each `compile_fail` doctest that breaks the contract."""
    defects: list[str] = []
    for group in doc_comment_groups(source):
        blocks = code_blocks(group)
        controls = [block for block in blocks if is_positive_control(block)]
        for fence in filter(is_compile_fail, blocks):
            defects.extend(_block_defects(fence, controls))
    return defects


def compile_fail_count(source: str) -> int:
    """Return how many `compile_fail` doctests a source file holds."""
    return sum(
        is_compile_fail(block)
        for group in doc_comment_groups(source)
        for block in code_blocks(group)
    )
