r"""CrossHair symbolic contracts for the ownership-aware fail-fast selection.

`cuprum._process_lifecycle._stages_to_terminate` is the pure selection behind
the fail-fast teardown: which stages a pipeline terminates after its first
failure. `test_subprocess_timeout_reducers.py` drives it with Hypothesis, and
`test_subprocess_timeout_reducers_crosshair.py` verifies the inherited rule
symbolically. This module adds the ownership-aware rule those two cannot see,
because once an owned stage exists the selection is no longer the inherited
rule — it is that rule unioned with every owned stage.

The split is by contract, not by size. The base module stays a faithful record
of the rule that shipped before ownership existed, so a regression there is
reported against that rule rather than against this one; the two families also
fail differently, and keeping them apart points a refutation at the rule that
actually broke.

The symbolic domain is deliberately small and finite so CrossHair confirms
rather than times out: pipelines are capped at three stages, `failure_index` is
constrained to a valid index by precondition, and completion and ownership are
each a single bounded integer bitmask rather than nested symbolic lists. That
last choice is what lets the three-stage space be exhausted.

Run these symbolically with either::

    uv run pytest cuprum/unittests/test_subprocess_timeout_reducers_ownership_*.py
    uv run crosshair check cuprum/unittests/test_subprocess_timeout_reducers_ownership_*

At import time this module probes CrossHair availability using the shared
helpers in `_crosshair_support.py`, matching `test_line_splitting.py`. Only a
missing dependency (``ImportError``) or a tracer that cannot handle the
interpreter (``TraceException``) degrades to a skip; every other failure, and
any available-but-unconfirmed CrossHair result, fails the test rather than
being downgraded to a warning.
"""

from __future__ import annotations

import importlib
import typing as typ

import pytest

from cuprum._process_lifecycle import _stages_to_terminate
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
    # it cannot be named here without re-triggering the failing import. The
    # shared helper re-raises control-flow exceptions and anything unexpected.
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


# The stage counts both contracts are stated over. Three is the largest that
# keeps the two masks a single small integer each, which is what makes the
# space exhaustive rather than merely sampled.
_MIN_STAGE_COUNT = 1
_MAX_STAGE_COUNT = 3


def _decoded_flags(stage_count: int, mask: int) -> list[bool]:
    """Decode ``mask`` into ``stage_count`` flags, least significant bit first."""
    return [bool((mask >> idx) & 1) for idx in range(stage_count)]


def _selection_is_well_shaped(selected: list[int], stage_count: int) -> bool:
    """Report whether a selection is in range, free of repeats, and ordered."""
    return (
        all(0 <= idx < stage_count for idx in selected)
        and len(selected) == len(set(selected))
        and selected == sorted(selected)
    )


def _expected_owned_selection(
    failure_index: int,
    done: list[bool],
    owns_group: list[bool],
) -> list[int]:
    """Return the stages the ownership-aware rule must select, in order.

    Returns
    -------
    list[int]
        The expected stage indices, in ascending order.
    """
    return [
        idx
        for idx, is_done in enumerate(done)
        if owns_group[idx] or (idx != failure_index and not is_done)
    ]


def _owned_selection_is_exact(
    selected: list[int],
    failure_index: int,
    done: list[bool],
    owns_group: list[bool],
) -> bool:
    """Report whether the selection is the inherited rule unioned with ownership.

    Stated as exact membership rather than as a chain of exclusions, so the
    predicate is refuted either by a stage the rule must select and does not,
    or by one it must not select and does. An owned stage is selected even when
    its wait task reports completion and even when it is the stage that failed:
    its group outlives the leader, and whether anything is actually signalled is
    decided later by the group probe.

    Returns
    -------
    bool
        Whether the selection matches the ownership-aware rule exactly.
    """
    return selected == _expected_owned_selection(failure_index, done, owns_group)


def _owned_selection_is_well_formed(
    stage_count: int,
    failure_index: int,
    masks: tuple[int, int],
) -> bool:
    """Report whether an ownership-aware selection is well shaped and exact.

    ``masks`` carries the completion and ownership bitmasks in that order.

    Returns
    -------
    bool
        Whether the selection is both well shaped and exactly the right set.
    """
    done_mask, owned_mask = masks
    done = _decoded_flags(stage_count, done_mask)
    owns_group = _decoded_flags(stage_count, owned_mask)
    selected = _stages_to_terminate(failure_index, done, owns_group=owns_group)
    return _selection_is_well_shaped(
        selected, stage_count
    ) and _owned_selection_is_exact(selected, failure_index, done, owns_group)


def _expected_prefix_selection(
    failure_index: int,
    done: list[bool],
    owns_group: list[bool],
    cut: int,
) -> list[int]:
    """Return the stages a prefix of ``owns_group`` of length ``cut`` selects.

    Returns
    -------
    list[int]
        The expected stage indices for that prefix, in ascending order.
    """
    return [
        idx
        for idx, is_done in enumerate(done)
        if (idx < cut and owns_group[idx]) or (idx != failure_index and not is_done)
    ]


def _short_ownership_degrades(
    prefixes: list[list[int]],
    failure_index: int,
    done: list[bool],
    owns_group: list[bool],
) -> bool:
    """Report whether every ownership prefix selects exactly its own rule."""
    return all(
        selected == _expected_prefix_selection(failure_index, done, owns_group, cut)
        for cut, selected in enumerate(prefixes)
    )


def _omitted_ownership_never_selects_a_settled_stage(
    prefixes: list[list[int]],
    failure_index: int,
    done: list[bool],
    owns_group: list[bool],
) -> bool:
    """Report whether no prefix selects a settled stage whose flag was omitted."""
    return all(
        idx not in selected
        for idx, is_done in enumerate(done)
        if is_done and idx != failure_index and not owns_group[idx]
        for selected in prefixes
    )


def _short_ownership_keeps_direct_child_rule(
    stage_count: int,
    failure_index: int,
    masks: tuple[int, int],
) -> bool:
    """Report whether omitted ownership leaves a stage on the direct-child rule.

    The prefixes over ``owns_group`` are nested, so a stage unselected under the
    longest prefix stays unselected under every shorter one. That monotonicity
    is the property the default rests on: shortening the sequence can only ever
    remove selections, never add one, so no stage is reached merely because its
    flag went missing. The final prefix is the empty sequence — the default a
    caller that supplies no ownership at all receives.

    Returns
    -------
    bool
        Whether every prefix selects exactly its own rule.
    """
    done_mask, owned_mask = masks
    done = _decoded_flags(stage_count, done_mask)
    owns_group = _decoded_flags(stage_count, owned_mask)
    prefixes = [
        _stages_to_terminate(failure_index, done, owns_group=owns_group[:cut])
        for cut in range(len(owns_group) + 1)
    ]
    return _short_ownership_degrades(
        prefixes, failure_index, done, owns_group
    ) and _omitted_ownership_never_selects_a_settled_stage(
        prefixes, failure_index, done, owns_group
    )


def _owned_selection_contract(
    stage_count: int,
    failure_index: int,
    masks: tuple[int, int],
) -> None:
    """CrossHair contract: the ownership-aware selection is exact and well shaped.

    ``masks`` carries the completion and ownership bitmasks in that order.
    Bounding each element keeps the symbolic input two small integers while the
    PEP316 ``post:`` stays a single line, which the CrossHair parser requires.

    Running this predicate against the pre-ownership rule refutes it, which is
    what makes it a contract on the new behaviour rather than on a shape both
    rules share.

    pre: 1 <= stage_count <= 3
    pre: 0 <= failure_index < stage_count
    pre: 0 <= masks[0] <= 7
    pre: 0 <= masks[1] <= 7
    post: _owned_selection_is_well_formed(stage_count, failure_index, masks)
    """


def _short_ownership_contract(
    stage_count: int,
    failure_index: int,
    masks: tuple[int, int],
) -> None:
    """CrossHair contract: an omitted ownership flag never adds a selection.

    pre: 1 <= stage_count <= 3
    pre: 0 <= failure_index < stage_count
    pre: 0 <= masks[0] <= 7
    pre: 0 <= masks[1] <= 7
    post: _short_ownership_keeps_direct_child_rule(stage_count, failure_index, masks)
    """


@pytest.mark.crosshair
@pytest.mark.timeout(360)
@pytest.mark.parametrize(
    "contract",
    [
        pytest.param(_owned_selection_contract, id="owned_selection_well_formed"),
        pytest.param(_short_ownership_contract, id="short_ownership_conservative"),
    ],
)
@pytest.mark.skipif(check_states is None, reason=_CROSSHAIR_UNAVAILABLE_REASON)
def test_crosshair_ownership_contracts(contract: cabc.Callable[..., None]) -> None:
    """Property: CrossHair symbolically verifies the ownership-aware selection.

    ``check_states`` requires ``MessageType.CONFIRMED``, so an
    available-but-unconfirmed result (``CANNOT_CONFIRM``) or a refuted
    postcondition (``POST_FAIL``) fails the test rather than being downgraded.
    ``per_condition_timeout`` is a wall-clock budget: these bounded spaces are
    exhausted in seconds, but under the parallel ``-n auto`` run the CrossHair
    worker competes for CPU, so the per-test timeout sits well above it.
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
