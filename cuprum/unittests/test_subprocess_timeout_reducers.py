"""Property tests for the subprocess timeout and fail-fast reducers.

Two pure reducers were extracted from temporal, branch-heavy code so their
decisions can be property-tested without process state or a clock:

- `_resolve_timeout_payload` (`cuprum._subprocess_timeout`) — the timeout-payload
  seam: it maps either timeout variant to one consistent
  `_SubprocessTimeoutDetails`, always carrying a concrete timeout.
- `_stages_to_terminate` (`cuprum._process_lifecycle`) — the fail-fast selection:
  which stages get a termination task, each at most once and never the failed or
  an already-finished stage.
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum._process_lifecycle import _stages_to_terminate
from cuprum._subprocess_timeout import (
    _resolve_timeout_payload,
    _SubprocessInvariantError,
    _SubprocessTimeoutDetails,
    _SubprocessTimeoutError,
    _TimeoutFallback,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_FINITE_FLOATS = st.floats(allow_nan=False, allow_infinity=False)
_OPTIONAL_TEXT = st.none() | st.text()


@st.composite
def _timeout_details(draw: st.DrawFn) -> _SubprocessTimeoutDetails:
    """Draw an arbitrary captured `_SubprocessTimeoutDetails`."""
    return _SubprocessTimeoutDetails(
        timeout=draw(_FINITE_FLOATS),
        stdout=draw(_OPTIONAL_TEXT),
        stderr=draw(_OPTIONAL_TEXT),
        exited_at=draw(_FINITE_FLOATS),
    )


@given(details=_timeout_details())
def test_resolve_uses_carried_payload_for_subprocess_timeout_error(
    *,
    details: _SubprocessTimeoutDetails,
) -> None:
    """A `_SubprocessTimeoutError`'s captured payload is used verbatim."""
    payload = _resolve_timeout_payload(
        _SubprocessTimeoutError(details),
        # A None configured timeout would raise for a bare TimeoutError, so the
        # absence of a raise proves the carried branch never touches the
        # fallback; ``== details`` proves nothing from it leaks into the result.
        _TimeoutFallback(
            configured_timeout=None,
            stdout="ignored fallback stdout",
            stderr="ignored fallback stderr",
            exited_at=-1.0,
        ),
    )
    assert payload == details, (
        "a carried _SubprocessTimeoutError payload must be returned verbatim, "
        "with no field taken from the fallback"
    )


@given(
    configured_timeout=_FINITE_FLOATS,
    stdout_text=_OPTIONAL_TEXT,
    stderr_text=_OPTIONAL_TEXT,
    exited_at=_FINITE_FLOATS,
)
def test_resolve_builds_consistent_payload_for_bare_timeout(
    *,
    configured_timeout: float,
    stdout_text: str | None,
    stderr_text: str | None,
    exited_at: float,
) -> None:
    """A bare `TimeoutError` is resolved from the configured timeout and clock."""
    payload = _resolve_timeout_payload(
        TimeoutError(),
        _TimeoutFallback(
            configured_timeout=configured_timeout,
            stdout=stdout_text,
            stderr=stderr_text,
            exited_at=exited_at,
        ),
    )
    assert payload.timeout == configured_timeout, (
        "a bare TimeoutError must take its timeout from the fallback"
    )
    assert payload.exited_at == exited_at, (
        "a bare TimeoutError must take its exit time from the fallback"
    )
    assert payload.stdout == stdout_text, (
        "a bare TimeoutError must take its stdout from the fallback"
    )
    assert payload.stderr == stderr_text, (
        "a bare TimeoutError must take its stderr from the fallback"
    )


def test_resolve_bare_timeout_without_configured_timeout_is_an_invariant_error() -> (
    None
):
    """A bare `TimeoutError` with no configured timeout is an internal invariant."""
    with pytest.raises(_SubprocessInvariantError):
        _resolve_timeout_payload(
            TimeoutError(),
            _TimeoutFallback(
                configured_timeout=None,
                stdout=None,
                stderr=None,
                exited_at=0.0,
            ),
        )


@st.composite
def _termination_scenario(draw: st.DrawFn) -> tuple[int, list[bool]]:
    """Draw a ``(failure_index, done_flags)`` pair over a 1..10-stage pipeline."""
    stage_count = draw(st.integers(min_value=1, max_value=10))
    failure_index = draw(st.integers(min_value=0, max_value=stage_count - 1))
    done = draw(
        st.lists(st.booleans(), min_size=stage_count, max_size=stage_count),
    )
    return failure_index, done


@given(scenario=_termination_scenario())
def test_stages_to_terminate_selects_running_non_failed_stages(
    *,
    scenario: tuple[int, list[bool]],
) -> None:
    """Exactly the running, non-failed stages are selected, each once."""
    failure_index, done = scenario
    result = _stages_to_terminate(failure_index, done)

    expected = [
        idx for idx in range(len(done)) if idx != failure_index and not done[idx]
    ]
    assert result == expected, (
        "the selection must be exactly the unfinished, non-failed stages"
    )
    assert failure_index not in result, (
        "the failed stage owns its own exit and must never be terminated"
    )
    assert all(not done[idx] for idx in result), (
        "an already-finished stage needs no termination"
    )
    # No double-termination scheduling: indices are unique and ordered.
    assert len(result) == len(set(result)), (
        "no stage may be scheduled for termination twice"
    )
    assert result == sorted(result), "the selection must stay in ascending stage order"


@given(scenario=_termination_scenario())
def test_stages_to_terminate_is_idempotent(
    *,
    scenario: tuple[int, list[bool]],
) -> None:
    """Once the selected stages finish, a second pass schedules nothing."""
    failure_index, done = scenario
    first = _stages_to_terminate(failure_index, done)

    # Terminating a stage settles its wait task; re-running over the settled
    # state must select nothing new.
    settled: cabc.Sequence[bool] = [
        is_done or idx in first for idx, is_done in enumerate(done)
    ]
    assert _stages_to_terminate(failure_index, settled) == [], (
        "cleanup must be idempotent: once the selected stages have settled, "
        "a second pass must select nothing"
    )


@st.composite
def _owned_termination_scenario(
    draw: st.DrawFn,
) -> tuple[int, list[bool], list[bool]]:
    """Draw a scenario whose stages each carry an independent ownership flag."""
    failure_index, done = draw(_termination_scenario())
    owns_group = draw(
        st.lists(st.booleans(), min_size=len(done), max_size=len(done)),
    )
    return failure_index, done, owns_group


def _must_be_selected(idx: int, failure_index: int, done: list[bool]) -> bool:
    """Return whether stage ``idx`` must be selected under the inherited rule."""
    return idx != failure_index and not done[idx]


def _settled_and_not_failed(
    idx: int,
    failure_index: int,
    done: list[bool],
) -> bool:
    """Return whether stage ``idx`` has settled and is not the stage that failed."""
    return done[idx] and idx != failure_index


def _is_recorded_as_owner(idx: int, truncated: list[bool]) -> bool:
    """Return whether a truncated sequence records stage ``idx`` as an owner."""
    return idx < len(truncated) and truncated[idx]


@given(scenario=_owned_termination_scenario())
def test_owned_stages_are_selectable_whatever_their_waiter_says(
    *,
    scenario: tuple[int, list[bool], list[bool]],
) -> None:
    """Ownership widens the selection to every owned stage, and adds nothing else.

    An owned stage's group can outlive the leader that named it, so a completed
    wait task is not settlement: the descendant the stage left behind is still
    in that group, still holding the run's inherited pipes. The rule is
    therefore stated as a union rather than a replacement — the inherited
    selection is unchanged for a stage that owns nothing, and an owned stage is
    selected even when its waiter has already completed.

    The failed stage is included when it owns a group, and that is deliberate.
    It is the stage the run is reacting to and the one most likely to have
    started something it did not finish; whether anything is actually signalled
    is decided later, by the group probe, which answers for the group rather
    than for the leader. Excluding it here would suppress the teardown of the
    very group that most needs settling.
    """
    failure_index, done, owns_group = scenario
    result = _stages_to_terminate(failure_index, done, owns_group=owns_group)

    expected = [
        idx
        for idx, is_done in enumerate(done)
        if owns_group[idx] or (idx != failure_index and not is_done)
    ]
    assert result == expected, (
        "the selection must be the inherited rule unioned with every owned "
        f"stage; got {result!r}"
    )
    for idx, owned in enumerate(owns_group):
        if owned:
            assert idx in result, (
                f"an owned stage ({idx}) must stay selectable whatever its "
                "wait task reports, because its group outlives its leader"
            )
    for idx in range(len(done)):
        if not owns_group[idx] and _must_be_selected(idx, failure_index, done):
            assert idx in result, (
                "a running inherited stage must still be selected exactly as "
                "it was before ownership existed"
            )
    assert len(result) == len(set(result)), (
        "no stage may be scheduled for termination twice"
    )
    assert result == sorted(result), "the selection must stay in ascending stage order"


@given(scenario=_owned_termination_scenario())
def test_a_short_ownership_sequence_degrades_to_the_direct_child_rule(
    *,
    scenario: tuple[int, list[bool], list[bool]],
) -> None:
    """Ownership the caller did not supply must not be inferred as ownership.

    The sequence is per stage and is read positionally, so a caller that
    supplies fewer flags than stages — or none at all, the default — leaves the
    remaining stages on the direct-child rule. That is the conservative
    direction: a bookkeeping slip costs a descendant that should have been
    reclaimed, whereas padding with `True` would aim a group signal at a group
    the run may not have created.
    """
    failure_index, done, owns_group = scenario
    # Valid Python slices: `[:k]` for a prefix, and a deliberately short list
    # where the drawn one is already shortest.
    truncated = owns_group[: max(0, len(owns_group) - 1)]

    result = _stages_to_terminate(failure_index, done, owns_group=truncated)

    expected = [
        idx
        for idx, is_done in enumerate(done)
        if _is_recorded_as_owner(idx, truncated)
        or (idx != failure_index and not is_done)
    ]
    assert result == expected, (
        "a truncated ownership sequence must leave the remaining stages on "
        f"the direct-child rule; got {result!r}"
    )
    # The degradation has to be visible, not merely permitted. A stage whose
    # ownership flag was dropped and whose waiter has completed is the exact
    # case the conservative default decides: with its flag supplied it is
    # selectable, and with the flag omitted it is not.
    for idx in range(len(truncated), len(done)):
        if owns_group[idx] and _settled_and_not_failed(idx, failure_index, done):
            assert idx not in result, (
                f"stage {idx} owns a group in fact, but its flag was omitted, "
                "so the direct-child rule must leave it unselected rather than "
                "guess an ownership the caller did not record"
            )
