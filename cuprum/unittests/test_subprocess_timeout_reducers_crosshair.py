r"""CrossHair symbolic contracts for the timeout and fail-fast reducers.

Two pure reducers back the temporal, branch-heavy paths in issue `#75`:

- `cuprum._subprocess_timeout._resolve_timeout_payload` — the timeout-payload
  seam, mapping either timeout variant to one consistent
  `_SubprocessTimeoutDetails`.
- `cuprum._process_lifecycle._stages_to_terminate` — the fail-fast selection,
  choosing which stages a pipeline terminates after its first failure.

`test_subprocess_timeout_reducers.py` drives both with Hypothesis. The contracts
here verify the same invariants symbolically over a bounded state space, so a
confirmed result means the invariant held for every state CrossHair explored
rather than for the values Hypothesis happened to sample. The two layers are
complementary and both are kept.

The symbolic domains are deliberately small and finite so CrossHair can confirm
them rather than time out:

- pipelines are capped at three stages, with `failure_index` constrained to a
  valid index by precondition;
- timeouts and exit times are drawn from a small enumerated set rather than
  unrestricted floats, since the reducer only ever copies them;
- stdout and stderr are drawn from a short enumeration that includes ``None``,
  preserving the distinction between a carried value and a fallback value so a
  reducer that returned the wrong one would be caught.

Run these symbolically with either::

    uv run pytest cuprum/unittests/test_subprocess_timeout_reducers_crosshair.py
    uv run crosshair check \\
        cuprum/unittests/test_subprocess_timeout_reducers_crosshair.py \\
        --analysis_kind=PEP316

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
from cuprum._subprocess_timeout import (
    _resolve_timeout_payload,
    _SubprocessInvariantError,
    _SubprocessTimeoutDetails,
    _SubprocessTimeoutError,
    _TimeoutFallback,
)
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


# Small finite domains. The reducers only copy these values, so enumerating a
# few representatives is enough to distinguish a carried value from a fallback
# without forcing CrossHair to reason about unrestricted floats or strings.
_TIMES: tuple[float, ...] = (0.0, 1.5, 9.0)
_TEXTS: tuple[str | None, ...] = (None, "carried", "fallback")


def _carried_payload_wins(
    carried_picks: tuple[int, int, int, int], fb_pick: int
) -> bool:
    """Report whether a carried payload is returned verbatim.

    ``carried_picks`` indexes the enumerated domains in field order —
    ``(timeout, stdout, stderr, exited_at)`` — keeping the four values that
    describe one payload together. The fallback deliberately carries different
    values and a ``None`` configured timeout: a resolver that consulted it
    would either return the wrong field or raise.
    """
    # ``carried_picks`` indexes the enumerated domains in field order —
    # (timeout, stdout, stderr, exited_at) — keeping the four values that
    # describe one payload together. The fallback deliberately carries
    # different values and a None configured timeout: a resolver that consulted
    # it would either return the wrong field or raise.
    timeout_pick, stdout_pick, stderr_pick, exited_pick = carried_picks
    carried = _SubprocessTimeoutDetails(
        timeout=_TIMES[timeout_pick],
        stdout=_TEXTS[stdout_pick],
        stderr=_TEXTS[stderr_pick],
        exited_at=_TIMES[exited_pick],
    )
    payload = _resolve_timeout_payload(
        _SubprocessTimeoutError(carried),
        _TimeoutFallback(
            configured_timeout=None,
            stdout=_TEXTS[fb_pick],
            stderr=_TEXTS[fb_pick],
            exited_at=_TIMES[fb_pick],
        ),
    )
    return (
        payload.timeout == carried.timeout
        and payload.stdout == carried.stdout
        and payload.stderr == carried.stderr
        and payload.exited_at == carried.exited_at
    )


def _fallback_payload_used(
    timeout_pick: int,
    stdout_pick: int,
    stderr_pick: int,
    exited_pick: int,
) -> bool:
    """Report whether a bare timeout resolves exactly from the fallback."""
    fallback = _TimeoutFallback(
        configured_timeout=_TIMES[timeout_pick],
        stdout=_TEXTS[stdout_pick],
        stderr=_TEXTS[stderr_pick],
        exited_at=_TIMES[exited_pick],
    )
    payload = _resolve_timeout_payload(TimeoutError(), fallback)
    return (
        payload.timeout == fallback.configured_timeout
        and payload.stdout == fallback.stdout
        and payload.stderr == fallback.stderr
        and payload.exited_at == fallback.exited_at
    )


def _missing_timeout_raises(stdout_pick: int, exited_pick: int) -> bool:
    """Report whether a bare timeout without a configured timeout raises."""
    fallback = _TimeoutFallback(
        configured_timeout=None,
        stdout=_TEXTS[stdout_pick],
        stderr=_TEXTS[stdout_pick],
        exited_at=_TIMES[exited_pick],
    )
    try:
        _resolve_timeout_payload(TimeoutError(), fallback)
    except _SubprocessInvariantError:
        return True
    return False


def _done_flags(stage_count: int, mask: int) -> list[bool]:
    """Decode ``mask`` into ``stage_count`` completion flags.

    Enumerating the flags as an integer bitmask keeps the symbolic input a
    single bounded integer rather than a symbolic list of symbolic booleans,
    which is what lets CrossHair exhaust the space.
    """
    # Enumerating the flags as an integer bitmask keeps the symbolic input a
    # single bounded integer rather than a symbolic list of symbolic booleans,
    # which is what lets CrossHair exhaust the space.
    return [bool((mask >> idx) & 1) for idx in range(stage_count)]


def _selection_shape_is_valid(selected: list[int], stage_count: int) -> bool:
    """Report whether the selection is in range, free of repeats, and ordered."""
    return (
        all(0 <= idx < stage_count for idx in selected)
        and len(selected) == len(set(selected))
        and selected == sorted(selected)
    )


def _selection_membership_is_exact(
    selected: list[int],
    failure_index: int,
    done: list[bool],
) -> bool:
    """Report whether the selection is exactly the unfinished, non-failed set."""
    expected = [
        idx for idx in range(len(done)) if idx != failure_index and not done[idx]
    ]
    return (
        failure_index not in selected
        and all(done[idx] is False for idx in selected)
        and selected == expected
    )


def _selection_is_well_formed(stage_count: int, failure_index: int, mask: int) -> bool:
    """Report whether the selection is well shaped and exactly the right set."""
    done = _done_flags(stage_count, mask)
    selected = _stages_to_terminate(failure_index, done)
    return _selection_shape_is_valid(
        selected, stage_count
    ) and _selection_membership_is_exact(selected, failure_index, done)


def _selection_is_idempotent(stage_count: int, failure_index: int, mask: int) -> bool:
    """Report whether a second pass over settled stages selects nothing."""
    done = _done_flags(stage_count, mask)
    selected = _stages_to_terminate(failure_index, done)
    # Terminating a stage settles its wait task.
    settled: cabc.Sequence[bool] = [
        is_done or idx in selected for idx, is_done in enumerate(done)
    ]
    return _stages_to_terminate(failure_index, settled) == []


def _carried_payload_contract(picks: tuple[int, int, int, int], fb_i: int) -> None:
    """CrossHair contract: a carried payload is returned verbatim.

    ``picks`` indexes the carried payload's four fields in order; bounding each
    element individually preserves exactly the domain the former scalar
    parameters described.

    pre: 0 <= picks[0] <= 2
    pre: 0 <= picks[1] <= 2
    pre: 0 <= picks[2] <= 2
    pre: 0 <= picks[3] <= 2
    pre: 0 <= fb_i <= 2
    post: _carried_payload_wins(picks, fb_i)
    """


def _fallback_payload_contract(
    timeout_pick: int,
    stdout_pick: int,
    stderr_pick: int,
    exited_pick: int,
) -> None:
    """CrossHair contract: a bare timeout resolves from the fallback.

    pre: 0 <= timeout_pick <= 2
    pre: 0 <= stdout_pick <= 2
    pre: 0 <= stderr_pick <= 2
    pre: 0 <= exited_pick <= 2
    post: _fallback_payload_used(timeout_pick, stdout_pick, stderr_pick, exited_pick)
    """


def _missing_timeout_contract(stdout_pick: int, exited_pick: int) -> None:
    """CrossHair contract: a missing configured timeout is an invariant error.

    pre: 0 <= stdout_pick <= 2
    pre: 0 <= exited_pick <= 2
    post: _missing_timeout_raises(stdout_pick, exited_pick)
    """


def _selection_contract(stage_count: int, failure_index: int, mask: int) -> None:
    """CrossHair contract: the fail-fast selection is well formed.

    Covers in-range, uniqueness, ordering, exclusion of the failed stage,
    exclusion of finished stages, and exact membership in one predicate.

    pre: 1 <= stage_count <= 3
    pre: 0 <= failure_index < stage_count
    pre: 0 <= mask < 8
    post: _selection_is_well_formed(stage_count, failure_index, mask)
    """


def _selection_idempotence_contract(
    stage_count: int,
    failure_index: int,
    mask: int,
) -> None:
    """CrossHair contract: cleanup is idempotent.

    pre: 1 <= stage_count <= 3
    pre: 0 <= failure_index < stage_count
    pre: 0 <= mask < 8
    post: _selection_is_idempotent(stage_count, failure_index, mask)
    """


@pytest.mark.crosshair
@pytest.mark.timeout(360)
@pytest.mark.parametrize(
    "contract",
    [
        pytest.param(_carried_payload_contract, id="carried_payload"),
        pytest.param(_fallback_payload_contract, id="fallback_payload"),
        pytest.param(_missing_timeout_contract, id="missing_timeout_raises"),
        pytest.param(_selection_contract, id="selection_well_formed"),
        pytest.param(_selection_idempotence_contract, id="selection_idempotent"),
    ],
)
@pytest.mark.skipif(check_states is None, reason=_CROSSHAIR_UNAVAILABLE_REASON)
def test_crosshair_contracts(contract: cabc.Callable[..., None]) -> None:
    """Property: CrossHair symbolically verifies both reducers.

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
