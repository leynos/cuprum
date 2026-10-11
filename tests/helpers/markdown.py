"""Markdown helpers shared by documentation contract tests."""

from __future__ import annotations

import re

# The opener's indentation is captured rather than discarded: whether a marker
# really opens a fence depends on the container it sits in, which the pattern
# alone cannot express.
_FENCE = re.compile(r"^([ \t]*)(`{3,}|~{3,})(.*)$")

# A list marker and the whitespace after it. The gap decides the item's content
# indent, which is what a nested fence is measured against.
_LIST_ITEM = re.compile(r"^([ \t]*)((?:[-*+]|\d{1,9}[.)]))([ \t]+)")

# An opener may sit this many columns further in than the container it belongs
# to. CommonMark counts four, measured the same way, as indented code.
_MAX_OPENER_OFFSET = 3

# Up to four spaces after a list marker set the item's content indent; more
# means the item begins with indented code. A tab stop is the same width.
_SPACES_THAT_SET_CONTENT = 4

_TAB_STOP = 4


def _columns(prefix: str) -> int:
    """Return *prefix*'s width in columns, expanding tabs to four-column stops."""
    width = 0
    for char in prefix:
        width = width + 1 if char == " " else (width // _TAB_STOP + 1) * _TAB_STOP
    return width


def _content_indent(line: str) -> int | None:
    """Return the content indent of the list item *line* opens, if it opens one."""
    match = _LIST_ITEM.match(line)
    if match is None:
        return None
    base = _columns(match.group(1))
    marker = len(match.group(2))
    gap = _columns(match.group(3))
    # More than the setter width means indented code, and content then sits one
    # space in from the marker rather than at the far edge of the gap.
    if gap > _SPACES_THAT_SET_CONTENT:
        gap = 1
    return base + marker + gap


def _leading_indent(line: str) -> int:
    """Return the width of *line*'s leading whitespace."""
    return _columns(line[: len(line) - len(line.lstrip(" \t"))])


def _closes_fence(run: str, rest: str, opener: tuple[str, int]) -> bool:
    """Report whether *run* closes the CommonMark block *opener* began."""
    if run[0] != opener[0] or len(run) < opener[1]:
        return False
    return not rest.strip()


def _track_containers(containers: list[int], line: str) -> int:
    """Update the open list-item indents in place, returning the innermost.

    An item ends at the first line indented less far than its content, so lines
    that fall short of the innermost open item pop it. This runs only outside a
    fence: inside one, indentation is content and closes nothing.

    Returns
    -------
    int
        The content indent of the innermost open item, or ``0`` at top level.
    """
    indent = _leading_indent(line)
    if not line.strip():
        return containers[-1] if containers else 0
    while containers and indent < containers[-1]:
        containers.pop()
    return containers[-1] if containers else 0


def _opens_fence(container: int, line: str) -> re.Match[str] | None:
    """Return the opener match for *line*, or ``None`` if it does not open one.

    An opener may sit up to three columns further in than the container it
    belongs to. A top-level marker four spaces in is indented code, while the
    same marker four spaces in inside a list item is a real fence: the item's
    own content indent has already accounted for the difference, so only the
    offset from that indent decides.

    Returns
    -------
    re.Match of str or None
        The fence-run and info-string groups, or ``None`` when *line* does not
        open a fence.
    """
    match = _FENCE.match(line)
    if match is None:
        return None
    return None if _leading_indent(line) > container + _MAX_OPENER_OFFSET else match


def _step_fence(
    opener: tuple[str, int] | None,
    line: str,
    containers: list[int],
) -> tuple[tuple[str, int] | None, bool]:
    """Advance the fence state by one line, updating *containers* in place.

    The state is carried in and out rather than held by the caller, so the
    decision that changes it lives in one place: reading a fence line is the
    only thing that opens, closes, or leaves it alone. The open list items are
    tracked alongside it for the same reason — an opener's indentation can only
    be judged against the container the line is currently inside.

    Returns
    -------
    tuple of (optional tuple of (str, int), bool)
        The state after *line*, and whether the line lies inside a fence.
    """
    container = containers[-1] if containers else 0
    if opener is None:
        container = _track_containers(containers, line)

    match = _opens_fence(container, line)
    if match is None:
        if opener is None:
            content = _content_indent(line)
            if content is not None:
                containers.append(content)
            return None, False
        return opener, True

    run, rest = match.group(2), match.group(3)
    if opener is None:
        # A backtick opener whose info string contains a backtick is content,
        # per the CommonMark rule above, so it neither opens nor closes.
        if run[0] == "`" and "`" in rest:
            return None, False
        return (run[0], len(run)), True
    if _closes_fence(run, rest, opener):
        return None, True
    return opener, True


def _fenced_line_indexes(markdown: str) -> set[int]:
    """Return the indexes of the lines enclosed by fenced code blocks.

    A fence opens on a run of at least three backticks or tildes. Knowing which
    lines are fenced lets a caller pass over code without rewriting it, so the
    text it goes on to return is still the document's own.

    Closing follows CommonMark rather than simply matching the opening
    character. A closer is a run of the *same* character at least as long as the
    opener, with nothing but whitespace after it. A shorter run, or one with
    trailing text, is content: a four-backtick block may therefore contain a
    three-backtick line, which is exactly how a document nests one example
    inside another. Treating any same-character run as a closer ended the block
    early, so every line after it was misclassified — a heading inside the
    example became structure, and this helper's callers truncated the section
    there.

    An opening backtick fence whose info string contains a backtick is not a
    fence either, per the same rule; such a line is ordinary content.

    Indentation is measured against the enclosing container, not the document.
    An opener may sit up to three columns further in than the list item it
    belongs to, so a marker four spaces in is a fence inside an item whose
    content begins at one space, and indented code at the top level. Accepting
    any width instead let a top-level four-space marker open a fence, which
    swallowed the real heading after it and truncated the section every contract
    test reads. Requiring the document-absolute three spaces fixes that case but
    drops the list-nested one, because it cannot tell the two apart.

    Parameters
    ----------
    markdown : str
        The Markdown document to scan.

    Returns
    -------
    set of int
        Zero-based indexes of the fenced lines, including both fence markers.
    """
    fenced: set[int] = set()
    opener: tuple[str, int] | None = None
    containers: list[int] = []
    # Split on newlines rather than using ``str.splitlines``: the caller locates
    # a match with ``count("\n", ...)``, and ``splitlines`` also breaks on
    # form feed, vertical tab, and the Unicode line separators. Any of those in
    # a document would shift every index after it and desynchronize the two.
    for index, line in enumerate(markdown.split("\n")):
        opener, is_fenced = _step_fence(opener, line, containers)
        if is_fenced:
            fenced.add(index)
    return fenced


def _next_heading_start(markdown: str, *, level: int, start: int) -> int | None:
    """Return where the next heading of *level* or higher begins, if any.

    Only a heading a reader would see closes a section. A ``#`` line inside a
    fenced code block is comment or shell syntax, not structure, so it is passed
    over; without that, an example containing such a line would silently
    truncate the section every contract test reads.

    Parameters
    ----------
    markdown : str
        The Markdown document to search.
    level : int
        The heading level to match, as a count of ``#`` characters.
    start : int
        Offset at which to begin searching.

    Returns
    -------
    int or None
        The offset of the heading, or ``None`` when the section runs to the end
        of the document.
    """
    fenced = _fenced_line_indexes(markdown)
    heading = re.compile(rf"^#{{1,{level}}}\s", re.MULTILINE)
    for match in heading.finditer(markdown, start):
        line_index = markdown.count("\n", 0, match.start())
        if line_index not in fenced:
            return match.start()
    return None


def extract_markdown_subsection(markdown: str, *, heading: str, level: int = 3) -> str:
    """Return the content for a Markdown subsection until the next peer heading.

    Parameters
    ----------
    markdown : str
        The Markdown document searched for the subsection.
    heading : str
        The heading text whose body is extracted.
    level : int
        The heading level to match, as a count of ``#`` characters.
        Defaults to ``3``.

    Returns
    -------
    str
        The subsection body between ``heading`` and the next peer-or-higher
        heading. A heading inside a fenced code block does not close the
        section, and the body is returned verbatim.

    Raises
    ------
    AssertionError
        If no heading matching ``heading`` at ``level`` is found.
    """
    heading_line = "#" * level + f" {heading}"
    match = re.search(rf"^{re.escape(heading_line)}[ \t]*$", markdown, re.MULTILINE)
    if match is None:
        msg = f"missing subsection heading: {heading!r}"
        raise AssertionError(msg)
    newline = markdown.find("\n", match.end())
    body_start = len(markdown) if newline == -1 else newline + 1
    body_end = _next_heading_start(markdown, level=level, start=body_start)
    if body_end is None:
        return markdown[body_start:]
    return markdown[body_start:body_end]
