"""Property tests for deterministic literal match projection."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from cuprum.redaction import RedactionPolicy


def _literal_union_projection(text: str, literals: tuple[str, ...]) -> str:
    """Project the union of literal match ranges using a coverage oracle."""
    covered = [False] * len(text)
    for literal in literals:
        offset = 0
        while (start := text.find(literal, offset)) >= 0:
            for index in range(start, start + len(literal)):
                covered[index] = True
            offset = start + 1

    projected: list[str] = []
    index = 0
    while index < len(text):
        if not covered[index]:
            projected.append(text[index])
            index += 1
            continue
        while index < len(text) and covered[index]:
            index += 1
        projected.append("[REDACTED]")
    return "".join(projected)


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
