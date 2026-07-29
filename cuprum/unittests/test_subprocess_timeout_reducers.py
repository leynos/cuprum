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
