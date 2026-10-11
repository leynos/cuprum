"""Markdown helpers shared by documentation contract tests."""

from __future__ import annotations

import re

_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def _fenced_line_indexes(markdown: str) -> set[int]:
    """Return the indexes of the lines enclosed by fenced code blocks.

    A fence opens on a run of at least three backticks or tildes and closes on a
    run of the same character. Knowing which lines are fenced lets a caller pass
    over code without rewriting it, so the text it goes on to return is still
    the document's own.

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
    opener: str | None = None
    for index, line in enumerate(markdown.splitlines()):
        match = _FENCE.match(line)
        if match is None:
            if opener is not None:
                fenced.add(index)
            continue
        marker = match.group(1)[0]
        if opener is None:
            opener = marker
        elif marker == opener:
            opener = None
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
