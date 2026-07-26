"""Deeper property-based tests for folded-stack ranking.

The existing folded-summary tests pin parsing, total-sample accounting, and
inclusive-count deduplication. These properties go further into the ranking
reducer ``_rank_frames`` and the ``_FoldedSummaryState`` counters:

- Ranking order follows the composite key
  ``(-inclusive, -leaf, frame)`` — count descending, then leaf descending,
  then frame ascending.
- ``limit`` truncation keeps a stable prefix of the full ranking and never
  exceeds ``limit`` entries.
- Ranking is deterministic and each entry's fields mirror the counters.
- ``inclusive_samples >= leaf_samples`` for every frame.

``_percent`` additionally carries a pure bounds contract amenable to CrossHair;
run ``crosshair check
cuprum.unittests.test_folded_ranking_property._assert_percent_bounds
--analysis_kind asserts`` for symbolic verification.
"""

from __future__ import annotations

import collections
import typing as typ

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from benchmarks.summarize_folded import (
    _FoldedSummaryState,
    _percent,
    _rank_frames,
)

_FRAME = st.text(alphabet="abcd", min_size=1, max_size=2)
_FRAMES = st.lists(_FRAME, min_size=1, max_size=4).map(tuple)
_SAMPLES = st.integers(min_value=1, max_value=100)
_STACKS = st.lists(st.tuples(_FRAMES, _SAMPLES), min_size=1, max_size=6)


def _build_state(stacks: list[tuple[tuple[str, ...], int]]) -> _FoldedSummaryState:
    """Accumulate a folded-summary state from generated stacks."""
    state = _FoldedSummaryState(
        inclusive=collections.Counter(),
        leaf=collections.Counter(),
        stacks=collections.Counter(),
        examples={},
    )
    for frames, samples in stacks:
        state.add(frames, samples, example_limit=2)
    return state


def _inclusive_key(
    state: _FoldedSummaryState,
    frame: str,
) -> tuple[int, int, str]:
    """Return the composite ranking key used by ``_rank_frames``."""
    return (-state.inclusive[frame], -state.leaf[frame], frame)


@given(stacks=_STACKS)
def test_ranking_follows_composite_key(
    stacks: list[tuple[tuple[str, ...], int]],
) -> None:
    """Ranked frames are ordered by (-inclusive, -leaf, frame)."""
    state = _build_state(stacks)
    ranked = _rank_frames(state, state.inclusive, limit=len(state.inclusive) + 5)
    keys = [_inclusive_key(state, typ.cast("str", entry["frame"])) for entry in ranked]
    assert keys == sorted(keys), (
        "ranked frames must be ordered by (-inclusive, -leaf, frame)"
    )


@given(stacks=_STACKS, limit=st.integers(min_value=0, max_value=8))
def test_limit_truncates_a_stable_prefix(
    stacks: list[tuple[tuple[str, ...], int]],
    limit: int,
) -> None:
    """A limited ranking is exactly the prefix of the full ranking."""
    state = _build_state(stacks)
    full = _rank_frames(state, state.inclusive, limit=len(state.inclusive) + 5)
    limited = _rank_frames(state, state.inclusive, limit=limit)
    assert len(limited) <= limit, "a limited ranking must not exceed the limit"
    assert limited == full[:limit], (
        "a limited ranking must be the full ranking's prefix"
    )


@given(stacks=_STACKS)
def test_ranking_is_deterministic(
    stacks: list[tuple[tuple[str, ...], int]],
) -> None:
    """Ranking the same state twice yields identical output."""
    state = _build_state(stacks)
    first = _rank_frames(state, state.inclusive, limit=5)
    second = _rank_frames(state, state.inclusive, limit=5)
    assert first == second, "ranking must be deterministic for the same state"


@given(stacks=_STACKS)
def test_entry_fields_and_inclusive_dominates_leaf(
    stacks: list[tuple[tuple[str, ...], int]],
) -> None:
    """Entry counters mirror the state, and inclusive >= leaf per frame."""
    state = _build_state(stacks)
    ranked = _rank_frames(state, state.inclusive, limit=len(state.inclusive) + 5)
    for entry in ranked:
        frame = typ.cast("str", entry["frame"])
        inclusive_samples = typ.cast("int", entry["inclusive_samples"])
        leaf_samples = typ.cast("int", entry["leaf_samples"])
        assert inclusive_samples == state.inclusive[frame], (
            "inclusive_samples must mirror the inclusive counter"
        )
        assert leaf_samples == state.leaf[frame], (
            "leaf_samples must mirror the leaf counter"
        )
        assert entry["inclusive_percent"] == _percent(
            state.inclusive[frame], state.total
        ), "inclusive_percent must be derived from the inclusive counter"
        assert entry["leaf_percent"] == _percent(state.leaf[frame], state.total), (
            "leaf_percent must be derived from the leaf counter"
        )
        assert inclusive_samples >= leaf_samples, (
            "inclusive samples must dominate leaf samples per frame"
        )


# A representable ceiling for the CrossHair contract. Real sample tallies never
# approach this; beyond it ``samples * 100.0`` overflows float to infinity (and
# far larger ints raise ``OverflowError``), which would let CrossHair report a
# spurious counterexample rather than verify the intended [0, 100] bound.
_PERCENT_CONTRACT_MAX = 10**12


def _assert_percent_bounds(samples: int, total: int) -> None:
    """Contract: ``_percent`` stays within [0, 100] for valid sample counts."""
    if not 0 < total <= _PERCENT_CONTRACT_MAX:
        return
    if not 0 <= samples <= total:
        return
    result = _percent(samples, total)
    assert 0.0 <= result <= 100.0, (
        f"_percent({samples}, {total}) = {result} is outside [0, 100]"
    )


@pytest.mark.crosshair
@given(
    total=st.integers(min_value=1, max_value=10**6),
    samples=st.integers(min_value=0, max_value=10**6),
)
def test_percent_within_bounds(total: int, samples: int) -> None:
    """``_percent`` never leaves the [0, 100] range for valid counts."""
    assume(samples <= total)
    _assert_percent_bounds(samples, total)


def test_percent_of_empty_summary_is_zero() -> None:
    """An empty summary (``total == 0``) yields 0.0 rather than dividing by zero.

    The ``0 < total`` guard in the bounded contract and the strategy's
    ``min_value=1`` both exclude this branch, so it is pinned explicitly.
    """
    assert _percent(0, 0) == 0.0, "zero total must produce a 0.0 percentage"
