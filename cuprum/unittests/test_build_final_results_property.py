"""Property tests for compacting fail-fast concurrent command results.

Run ``crosshair check
cuprum.unittests.test_build_final_results_property._assert_build_final_results_invariants
--analysis_kind asserts`` for symbolic verification.
"""

from __future__ import annotations

import importlib.util

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum.concurrent import _build_final_results
from cuprum.sh import CommandResult, Program

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


def _build_final_results_invariants_hold(
    inputs: list[CommandResult | None],
) -> bool:
    """Return whether the reducer satisfies its compaction invariants."""
    final_results, failures = _build_final_results(inputs)
    expected_results = [result for result in inputs if result is not None]

    return (
        all(0 <= idx < len(final_results) for idx in failures)
        and all(not final_results[idx].ok for idx in failures)
        and failures == sorted(failures)
        and None not in final_results
        and len(final_results) == sum(1 for result in inputs if result is not None)
        and final_results == expected_results
    )


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
