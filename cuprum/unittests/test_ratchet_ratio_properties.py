"""Property-based tests for the ratchet ratio extraction invariants.

These complement the example-based ratchet suite with Hypothesis properties:
the extracted ratio map must be invariant under any permutation of the paired
scenario/result entries, each group's ratio must be exactly the Rust mean over
the Python mean, incomplete or duplicated groups must be rejected whatever the
surrounding data, and group matching must accept exactly the equal key sets.
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from benchmarks.ratchet_ratio_extraction import (
    _extract_rust_python_ratios,
    validate_matching_comparison_groups,
)

_comparison_ids = st.sets(
    st.text(alphabet="abc", min_size=1, max_size=4),
    min_size=1,
    max_size=4,
)
_means = st.floats(min_value=1e-3, max_value=1e3, allow_nan=False)


class _Group(typ.NamedTuple):
    """One comparison group's generated Python and Rust means."""

    comparison_id: str
    python_mean: float
    rust_mean: float


@st.composite
def _runs(draw: st.DrawFn) -> list[_Group]:
    """Generate a benchmark run as one Python/Rust mean pair per group."""
    ids = sorted(draw(_comparison_ids))
    return [_Group(comparison_id, draw(_means), draw(_means)) for comparison_id in ids]


def _payloads(
    entries: list[tuple[str, str, float]],
) -> tuple[dict[str, object], dict[str, object]]:
    """Build matched plan and throughput payloads from ordered entries."""
    return (
        {
            "scenarios": [
                {"name": f"{backend}-{comparison_id}", "backend": backend}
                for comparison_id, backend, _ in entries
            ]
        },
        {
            "results": [
                {"command": f"{backend}-{comparison_id}", "mean": mean}
                for comparison_id, backend, mean in entries
            ]
        },
    )


def _entries(groups: list[_Group]) -> list[tuple[str, str, float]]:
    """Flatten groups into per-backend entries in a canonical order."""
    return [
        entry
        for group in groups
        for entry in (
            (group.comparison_id, "python", group.python_mean),
            (group.comparison_id, "rust", group.rust_mean),
        )
    ]


@given(groups=_runs(), data=st.data())
@settings(max_examples=50)
def test_ratio_extraction_is_permutation_invariant(
    groups: list[_Group],
    data: st.DataObject,
) -> None:
    """Any ordering of the paired entries yields the same ratio map."""
    canonical = _entries(groups)
    shuffled = data.draw(st.permutations(canonical))

    plan, throughput = _payloads(canonical)
    expected = _extract_rust_python_ratios(
        plan_payload=plan,
        throughput_payload=throughput,
        context_name="baseline",
    )
    plan, throughput = _payloads(list(shuffled))
    permuted = _extract_rust_python_ratios(
        plan_payload=plan,
        throughput_payload=throughput,
        context_name="baseline",
    )

    assert permuted == expected, "extracted ratios must not depend on scenario ordering"
    assert expected == {
        group.comparison_id: group.rust_mean / group.python_mean for group in groups
    }, "each group's ratio must be exactly its Rust mean over its Python mean"
    assert list(expected) == sorted(expected), (
        "the ratio map must list comparison groups deterministically sorted"
    )


@given(groups=_runs(), data=st.data())
@settings(max_examples=50)
def test_incomplete_groups_are_rejected(
    groups: list[_Group],
    data: st.DataObject,
) -> None:
    """Dropping either backend from any group fails extraction loudly."""
    entries = _entries(groups)
    removed = data.draw(st.sampled_from(entries))
    remaining = [entry for entry in entries if entry != removed]

    plan, throughput = _payloads(remaining)
    with pytest.raises(ValueError, match="missing"):
        _extract_rust_python_ratios(
            plan_payload=plan,
            throughput_payload=throughput,
            context_name="candidate",
        )


@given(groups=_runs(), data=st.data())
@settings(max_examples=50)
def test_duplicate_backend_entries_are_rejected(
    groups: list[_Group],
    data: st.DataObject,
) -> None:
    """Repeating any backend entry within a group is rejected."""
    entries = _entries(groups)
    duplicated = data.draw(st.sampled_from(entries))

    plan, throughput = _payloads([*entries, duplicated])
    with pytest.raises(ValueError, match="duplicate"):
        _extract_rust_python_ratios(
            plan_payload=plan,
            throughput_payload=throughput,
            context_name="candidate",
        )


@given(
    shared=_comparison_ids,
    extra=st.sets(st.text(alphabet="xyz", min_size=1, max_size=4), max_size=3),
    ratio=_means,
)
@settings(max_examples=50)
def test_group_matching_accepts_exactly_equal_key_sets(
    shared: set[str],
    extra: set[str],
    ratio: float,
) -> None:
    """Group validation passes equal key sets and rejects any difference."""
    baseline = dict.fromkeys(shared, ratio)
    candidate = dict.fromkeys(shared, ratio)

    validate_matching_comparison_groups(
        baseline_ratios=baseline,
        candidate_ratios=candidate,
    )

    if extra:
        candidate |= dict.fromkeys(extra, ratio)
        with pytest.raises(ValueError, match="comparison groups must match"):
            validate_matching_comparison_groups(
                baseline_ratios=baseline,
                candidate_ratios=candidate,
            )
