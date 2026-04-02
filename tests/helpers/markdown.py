"""Markdown helpers shared by documentation contract tests."""

from __future__ import annotations

import re


def extract_markdown_subsection(markdown: str, *, heading: str, level: int = 3) -> str:
    """Return the content for a Markdown subsection until the next peer heading."""
    heading_pattern = re.escape("#" * level + f" {heading}")
    match = re.search(
        rf"(?ms)^{heading_pattern}\n(?P<section>.*?)(?=^#{{1,{level}}}\s|\Z)",
        markdown,
    )
    if match is None:
        msg = f"missing subsection heading: {heading!r}"
        raise AssertionError(msg)
    return match.group("section")
