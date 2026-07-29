"""CrossHair symbolic contracts for the pipeline completion transition.

`cuprum._pipeline_wait._PipelineWaitState` splits completion handling into a
command, `record_completion`, and a side-effect-free query,
`should_terminate_others`. The Hypothesis state machine in
`test_pipeline_wait.py` drives randomized completion orders; the contracts here
verify the same invariants symbolically over a bounded state space, so a
confirmed result means the invariant held for every state CrossHair explored
rather than for the orders Hypothesis happened to sample.

The symbolic model is deliberately small. It touches no asyncio task,
subprocess, or clock: the state is constructed directly with only the fields
the pure transition reads, stage counts and indexes are bounded by
preconditions, and completion timestamps are injected as plain floats. That
keeps the explored space finite and points any counterexample at the transition
rather than at runtime infrastructure.

Run these symbolically with either::

    uv run pytest cuprum/unittests/test_pipeline_wait_crosshair.py -m crosshair
    uv run crosshair check cuprum/unittests/test_pipeline_wait_crosshair.py

At import time this module probes CrossHair availability before collecting the
symbolic tests, mirroring `test_line_splitting.py`. Expected unavailability
covers a missing CrossHair dependency (``ImportError``) and tracer
incompatibility reported as a ``TraceException``-named ``BaseException`` during
import; control-flow exceptions and every other failure are re-raised so an
unexpected CrossHair break is never silently downgraded to a skip.
"""

from __future__ import annotations

import importlib
import typing as typ

import pytest

from cuprum._pipeline_wait import _PipelineWaitState
from cuprum.unittests._crosshair_support import (
    _crosshair_unavailable_symbols,
    _warn_crosshair_unavailable,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_CROSSHAIR_PROBE_EXCEPTIONS: tuple[type[BaseException], ...] = (BaseException,)


# CrossHair's C-level tracer must support every bytecode opcode the running
# interpreter emits, so importing the integration can raise
# ``crosshair.tracers.TraceException`` (not ``ImportError``) on an interpreter
# whose opcode set it does not yet handle — the ``CALL_KW`` gap on early 3.15
# betas (issue #109). Probe here and degrade to skipping rather than hard-coding
# a version gate that must be revised whenever CrossHair catches up.
try:
    importlib.import_module("crosshair.core_and_libs")
    from crosshair.options import AnalysisKind, AnalysisOptionSet
    from crosshair.statespace import MessageType
    from crosshair.test_util import check_states
except _CROSSHAIR_PROBE_EXCEPTIONS as _crosshair_exc:
    # ``crosshair.tracers.TraceException`` subclasses ``BaseException`` (not
    # ``Exception``) and is raised while importing the tracer module itself, so
    # it cannot be named here without re-triggering the failing import.
    # Re-raise control-flow exceptions and any unexpected failure so the probe
    # only degrades for the known CrossHair compatibility cases.
    (
        _CROSSHAIR_UNAVAILABLE_REASON,
        AnalysisOptionSet,
        AnalysisKind,
        MessageType,
        check_states,
    ) = _crosshair_unavailable_symbols(_crosshair_exc)
    _warn_crosshair_unavailable(_CROSSHAIR_UNAVAILABLE_REASON)
else:
    _CROSSHAIR_UNAVAILABLE_REASON = "CrossHair available"


def _symbolic_state(stage_count: int) -> _PipelineWaitState:
    """Build the minimal wait state the pure transition reads.

    The task fields stay empty because neither `record_completion` nor
    `should_terminate_others` consults them, which keeps asyncio out of the
    symbolic model entirely.
    """
    return _PipelineWaitState(
        wait_tasks=[],
        task_to_index={},
        exit_codes=[None] * stage_count,
        started_at=[0.0] * stage_count,
        ended_at=[None] * stage_count,
    )


def _writes_matching_slot(
    stage_count: int,
    completed_idx: int,
    exit_code: int,
    ended_at: float,
) -> bool:
    """Report whether a completion lands in its own slot and no other."""
    state = _symbolic_state(stage_count)
    state.record_completion(completed_idx, exit_code, ended_at=ended_at)
    others_untouched = all(
        state.exit_codes[idx] is None and state.ended_at[idx] is None
        for idx in range(stage_count)
        if idx != completed_idx
    )
    return (
        state.exit_codes[completed_idx] == exit_code
        and state.ended_at[completed_idx] == ended_at
        and others_untouched
    )


def _latches_first_failure(
    stage_count: int,
    first_completion: tuple[int, int],
    second_completion: tuple[int, int],
) -> bool:
    """Report whether the earliest non-zero completion owns ``failure_index``.

    Each completion is a ``(stage_index, exit_code)`` pair, and the parameter
    order is *completion* order: ``first_completion`` settles before
    ``second_completion``. Grouping the pair keeps the two values that describe
    one event together rather than spreading them across parallel arguments.
    """
    first_idx, first_exit = first_completion
    second_idx, second_exit = second_completion

    state = _symbolic_state(stage_count)
    state.record_completion(first_idx, first_exit, ended_at=1.0)
    state.record_completion(second_idx, second_exit, ended_at=2.0)

    if first_exit != 0:
        # The earlier completion failed, so it latches regardless of what the
        # later one did — including a later failure at a lower stage index.
        return state.failure_index == first_idx
    if second_exit != 0:
        return state.failure_index == second_idx
    return state.failure_index is None


def _query_matches_expected(
    stage_count: int,
    completed_idx: int,
    exit_code: int,
) -> bool:
    """Report whether the query holds exactly for a non-final first failure."""
    state = _symbolic_state(stage_count)
    state.record_completion(completed_idx, exit_code, ended_at=1.0)
    expected = exit_code != 0 and completed_idx != stage_count - 1
    return state.should_terminate_others(completed_idx) is expected


def _query_is_pure(stage_count: int, completed_idx: int, exit_code: int) -> bool:
    """Report whether repeated queries agree and leave the state unchanged."""
    state = _symbolic_state(stage_count)
    state.record_completion(completed_idx, exit_code, ended_at=1.0)

    before = (list(state.exit_codes), list(state.ended_at), state.failure_index)
    first = state.should_terminate_others(completed_idx)
    second = state.should_terminate_others(completed_idx)
    after = (list(state.exit_codes), list(state.ended_at), state.failure_index)
    return first == second and before == after


def _records_completion_contract(
    stages: int,
    idx: int,
    code: int,
    ended_at: float,
) -> None:
    """CrossHair contract for completion slot writes.

    pre: 1 <= stages <= 3
    pre: 0 <= idx < stages
    pre: -2 <= code <= 2
    pre: 0.0 <= ended_at <= 4.0
    post: _writes_matching_slot(stages, idx, code, ended_at)
    """


def _first_failure_latch_contract(
    stages: int,
    first_completion: tuple[int, int],
    second_completion: tuple[int, int],
) -> None:
    """CrossHair contract for first-failure latching in completion order.

    Covers both the latch and the no-relatch invariant: when the earlier
    completion already failed, the later one must not replace it.

    Each completion is a ``(stage_index, exit_code)`` pair, and the parameter
    order is completion order. The preconditions bound each element of the
    pairs individually, so the symbolic domain is exactly the one the previous
    scalar parameters described: two distinct valid stage indexes and two exit
    codes in ``-2..2``.

    pre: 2 <= stages <= 3
    pre: 0 <= first_completion[0] < stages
    pre: 0 <= second_completion[0] < stages
    pre: first_completion[0] != second_completion[0]
    pre: -2 <= first_completion[1] <= 2
    pre: -2 <= second_completion[1] <= 2
    post: _latches_first_failure(stages, first_completion, second_completion)
    """


def _should_terminate_others_contract(stages: int, idx: int, code: int) -> None:
    """CrossHair contract for the fail-fast query.

    Bounding ``stage_count`` from 1 covers the single-stage pipeline, and
    allowing ``completed_idx`` to reach ``stage_count - 1`` covers the
    final-stage failure; both must answer ``False``.

    pre: 1 <= stages <= 3
    pre: 0 <= idx < stages
    pre: -2 <= code <= 2
    post: _query_matches_expected(stages, idx, code)
    """


def _query_purity_contract(stages: int, idx: int, code: int) -> None:
    """CrossHair contract for query purity.

    pre: 1 <= stages <= 3
    pre: 0 <= idx < stages
    pre: -2 <= code <= 2
    post: _query_is_pure(stages, idx, code)
    """


@pytest.mark.crosshair
@pytest.mark.timeout(240)
@pytest.mark.parametrize(
    "contract",
    [
        pytest.param(_records_completion_contract, id="records_completion"),
        pytest.param(_first_failure_latch_contract, id="first_failure_latch"),
        pytest.param(_should_terminate_others_contract, id="should_terminate_others"),
        pytest.param(_query_purity_contract, id="query_purity"),
    ],
)
@pytest.mark.skipif(check_states is None, reason=_CROSSHAIR_UNAVAILABLE_REASON)
def test_crosshair_contracts(contract: cabc.Callable[..., None]) -> None:
    """Property: CrossHair symbolically verifies the completion transition.

    ``per_condition_timeout`` is a wall-clock budget rather than a step count.
    These bounded spaces are exhausted in a few CPU-seconds, but under the
    parallel ``-n auto`` run the CrossHair worker competes for CPU, so an
    over-tight budget yields a flaky ``CANNOT_CONFIRM``; the per-test timeout
    sits above the budget to accommodate that worst case.
    """
    if check_states is None:
        pytest.skip(_CROSSHAIR_UNAVAILABLE_REASON)
    check_states(
        contract,
        MessageType.CONFIRMED,
        AnalysisOptionSet(
            analysis_kind=(AnalysisKind.PEP316,),
            per_condition_timeout=60,
        ),
    )
