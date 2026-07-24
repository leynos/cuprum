"""Property tests for compacting fail-fast concurrent command results.

Run ``crosshair check
cuprum.unittests.test_build_final_results_property._assert_build_final_results_invariants
--analysis_kind asserts`` for symbolic verification.
"""

from __future__ import annotations

import importlib.util
import sys

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum.concurrent import _build_final_results
from cuprum.sh import CommandResult, Program

if sys.version_info >= (3, 15):
    pytest.skip(
        "CrossHair does not support Python 3.15 CALL_KW tracing yet; see #109.",
        allow_module_level=True,
    )

_MAX_TEXT_SIZE: int = 8
_MAX_ARGV_SIZE: int = 4
_MAX_RESULTS_SIZE: int = 8


def _load_crosshair_hypothesis() -> None:
    """Load CrossHair's Hypothesis integration when the package provides it."""
    if importlib.util.find_spec("crosshair.hypothesis") is not None:
        importlib.import_module("crosshair.hypothesis")


_load_crosshair_hypothesis()


@st.composite
def command_results(draw: st.DrawFn) -> CommandResult:
    """Build compact ``CommandResult`` instances for reducer properties."""
    return CommandResult(
        program=Program(draw(st.text(min_size=1, max_size=_MAX_TEXT_SIZE))),
        argv=draw(
            st.lists(
                st.text(max_size=_MAX_TEXT_SIZE),
                max_size=_MAX_ARGV_SIZE,
            ).map(tuple),
        ),
        exit_code=draw(
            st.one_of(
                st.sampled_from([-1, 0, 1]),
                st.integers(min_value=-128, max_value=255),
            ),
        ),
        pid=draw(st.one_of(st.just(-1), st.integers(min_value=0, max_value=4096))),
        stdout=draw(st.one_of(st.none(), st.text(max_size=_MAX_TEXT_SIZE))),
        stderr=draw(st.one_of(st.none(), st.text(max_size=_MAX_TEXT_SIZE))),
    )


@st.composite
def partial_results_lists(draw: st.DrawFn) -> list[CommandResult | None]:
    """Build partial result lists with completed and cancelled entries."""
    return draw(
        st.lists(
            st.one_of(st.none(), command_results()),
            max_size=_MAX_RESULTS_SIZE,
        ),
    )


def _failure_indices_in_bounds(
    failures: list[int],
    final_results: list[CommandResult],
) -> bool:
    """Return whether every failure index addresses the compacted result list."""
    return all(0 <= idx < len(final_results) for idx in failures)


def _failure_indices_point_to_failures(
    failures: list[int],
    final_results: list[CommandResult],
) -> bool:
    """Return whether every failure index points at a failed command result."""
    return all(not final_results[idx].ok for idx in failures)


def _failure_indices_sorted(failures: list[int]) -> bool:
    """Return whether failure indices are sorted in ascending order."""
    return failures == sorted(failures)


def _failures_are_exhaustive(
    failures: list[int],
    final_results: list[CommandResult],
) -> bool:
    """Return whether failures contains exactly the indices of every non-ok result."""
    return failures == sorted(i for i, r in enumerate(final_results) if not r.ok)


def _no_cancelled_entries(final_results: list[CommandResult]) -> bool:
    """Return whether compacted results contain no cancelled entries."""
    return None not in final_results


def _result_count_matches(
    final_results: list[CommandResult],
    inputs: list[CommandResult | None],
) -> bool:
    """Return whether compacted result count matches completed inputs."""
    return len(final_results) == sum(1 for r in inputs if r is not None)


def _order_preserved(
    final_results: list[CommandResult],
    expected_results: list[CommandResult],
) -> bool:
    """Return whether compacted results preserve input order."""
    return final_results == expected_results


def _submission_indices_preserved(
    submission_indices: list[int],
    inputs: list[CommandResult | None],
) -> bool:
    """Return whether compacted results retain original submission positions."""
    expected_indices = [
        index for index, result in enumerate(inputs) if result is not None
    ]
    return submission_indices == expected_indices


def _compaction_and_failure_invariants_hold(
    final_results: list[CommandResult],
    failures: list[int],
    inputs: list[CommandResult | None],
    expected_results: list[CommandResult],
) -> bool:
    """Return whether the compaction and failure-index invariants hold."""
    return (
        _failure_indices_in_bounds(failures, final_results)
        and _failure_indices_point_to_failures(failures, final_results)
        and _failure_indices_sorted(failures)
        and _failures_are_exhaustive(failures, final_results)
        and _no_cancelled_entries(final_results)
        and _result_count_matches(final_results, inputs)
        and _order_preserved(final_results, expected_results)
    )


def _build_final_results_invariants_hold(
    inputs: list[CommandResult | None],
) -> bool:
    """Return whether the reducer satisfies its compaction invariants."""
    final_results, submission_indices, failures = _build_final_results(inputs)
    expected_results = [result for result in inputs if result is not None]

    return _compaction_and_failure_invariants_hold(
        final_results, failures, inputs, expected_results
    ) and _submission_indices_preserved(submission_indices, inputs)


def _assert_build_final_results_invariants(
    inputs: list[CommandResult | None],
) -> None:
    """Assert the reducer's compacted output and failure-index invariants."""
    assert _build_final_results_invariants_hold(inputs), (
        f"expected compacted output and failure-index invariants to hold for {inputs!r}"
    )


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(inputs=partial_results_lists())
def test_build_final_results_preserves_compaction_invariants(
    inputs: list[CommandResult | None],
) -> None:
    """Reducer output contains compacted results and valid failure indices."""
    _assert_build_final_results_invariants(inputs)
