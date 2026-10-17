"""Property tests for deterministic literal match projection."""

from __future__ import annotations

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
