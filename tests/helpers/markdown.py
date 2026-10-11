"""Markdown helpers shared by documentation contract tests."""

from __future__ import annotations

import re

_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")


def _closes_fence(run: str, rest: str, opener: tuple[str, int]) -> bool:
    """Report whether *run* closes the CommonMark block *opener* began."""
    if run[0] != opener[0] or len(run) < opener[1]:
        return False
    return not rest.strip()


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
    # Split on newlines rather than using ``str.splitlines``: the caller locates
    # a match with ``count("\n", ...)``, and ``splitlines`` also breaks on
    # form feed, vertical tab, and the Unicode line separators. Any of those in
    # a document would shift every index after it and desynchronize the two.
    for index, line in enumerate(markdown.split("\n")):
        match = _FENCE.match(line)
        if match is None:
            if opener is not None:
                fenced.add(index)
            continue
        run, rest = match.group(1), match.group(2)
        if opener is None:
            if run[0] == "`" and "`" in rest:
                continue
            opener = (run[0], len(run))
        elif _closes_fence(run, rest, opener):
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
