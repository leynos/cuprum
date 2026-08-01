"""Markdown helpers shared by documentation contract tests."""

from __future__ import annotations

import re


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
        heading.

    Raises
    ------
    AssertionError
        If no heading matching ``heading`` at ``level`` is found.
    """
    heading_pattern = re.escape("#" * level + f" {heading}")
    match = re.search(
        rf"(?ms)^{heading_pattern}\n(?P<section>.*?)(?=^#{{1,{level}}}\s|\Z)",
        markdown,
    )
    if match is None:
        msg = f"missing subsection heading: {heading!r}"
        raise AssertionError(msg)
    return match.group("section")
