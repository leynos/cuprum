"""Property tests for deterministic literal match projection."""

from __future__ import annotations

import re

from hypothesis import given
from hypothesis import strategies as st

from cuprum.redaction import RedactionPolicy


def _literal_union_projection(text: str, literals: tuple[str, ...]) -> str:
    """Project the union of literal match ranges using a coverage oracle."""
    covered = [False] * len(text)
    for literal in literals:
        _mark_literal_matches(text, literal, covered)

    projected: list[str] = []
    previous_was_covered = False
    for character, is_covered in zip(text, covered, strict=True):
        if is_covered and previous_was_covered:
            continue
        projected.append("[REDACTED]" if is_covered else character)
        previous_was_covered = is_covered
    return "".join(projected)


def _mark_literal_matches(text: str, literal: str, covered: list[bool]) -> None:
    """Mark every possibly overlapping occurrence in the coverage oracle."""
    offset = 0
    while (start := text.find(literal, offset)) >= 0:
        covered[start : start + len(literal)] = [True] * len(literal)
        offset = start + 1


def _regex_union_projection(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
) -> str:
    """Project regex matches through an independent character-coverage oracle."""
    spans = tuple(
        match.span() for pattern in patterns for match in pattern.finditer(text)
    )
    nonzero_spans = tuple((start, end) for start, end in spans if start < end)
    covered = [False] * len(text)
    for start, end in nonzero_spans:
        covered[start:end] = [True] * (end - start)

    standalone_zero_positions = {
        start
        for start, end in spans
        if start == end
        and not any(
            span_start <= start <= span_end for span_start, span_end in nonzero_spans
        )
    }
    projected: list[str] = []
    previous_character_was_covered = False
    for position in range(len(text) + 1):
        if position in standalone_zero_positions:
            projected.append("[REDACTED]")
        if position == len(text):
            break
        is_covered = covered[position]
        if is_covered and previous_character_was_covered:
            continue
        projected.append("[REDACTED]" if is_covered else text[position])
        previous_character_was_covered = is_covered
    return "".join(projected)


@st.composite
def _regex_projection_cases(
    draw: st.DrawFn,
) -> tuple[str, tuple[re.Pattern[str], ...]]:
    """Generate regex cases with adjacent, overlapping and zero-width matches."""
    fragment = draw(st.text(alphabet="ab", min_size=1, max_size=3))
    prefix = draw(st.text(alphabet="ab", max_size=8))
    suffix = draw(st.text(alphabet="ab", max_size=8))
    text = prefix + fragment + fragment[0] + fragment + suffix
    patterns = (
        re.compile(re.escape(fragment)),
        re.compile(re.escape(fragment + fragment[0])),
        re.compile(rf"(?={re.escape(fragment)})"),
        re.compile(r"(?=.)|(?=$)"),
    )
    return text, tuple(draw(st.permutations(patterns)))


@given(
    text=st.text(alphabet="abc", max_size=24),
    literals=st.lists(
        st.text(alphabet="abc", min_size=1, max_size=4),
        min_size=1,
        max_size=5,
        unique=True,
    ).map(tuple),
)
def test_literal_policy_matches_union_oracle_and_is_order_independent(
    text: str,
    literals: tuple[str, ...],
) -> None:
    """Overlapping and adjacent matches replace each covered run once."""
    forward = RedactionPolicy(literals=literals).apply_text(text)
    reverse = RedactionPolicy(literals=tuple(reversed(literals))).apply_text(text)

    assert forward == _literal_union_projection(text, literals), (
        "the policy must replace the complete union of all original match spans"
    )
    assert reverse == forward, "literal rule ordering must not change projection"


@given(case=_regex_projection_cases())
def test_regex_policy_matches_union_oracle_and_is_order_independent(
    case: tuple[str, tuple[re.Pattern[str], ...]],
) -> None:
    """Regex matches redact their original-span union regardless of rule order."""
    text, patterns = case
    forward = RedactionPolicy(patterns=patterns).apply_text(text)
    reverse = RedactionPolicy(patterns=tuple(reversed(patterns))).apply_text(text)

    assert forward == _regex_union_projection(text, patterns), (
        "regex matches must replace the complete union of original source spans"
    )
    assert reverse == forward, "regex rule ordering must not change projection"
