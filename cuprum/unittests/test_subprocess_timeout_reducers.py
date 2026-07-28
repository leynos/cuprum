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

_finite_floats = st.floats(allow_nan=False, allow_infinity=False)
_optional_text = st.none() | st.text()


@given(
    timeout=_finite_floats,
    stdout=_optional_text,
    stderr=_optional_text,
    exited_at=_finite_floats,
    injected_now=_finite_floats,
    ctx_stdout=_optional_text,
    ctx_stderr=_optional_text,
)
def test_resolve_uses_carried_payload_for_subprocess_timeout_error(
    *,
    timeout: float,
    stdout: str | None,
    stderr: str | None,
    exited_at: float,
    injected_now: float,
    ctx_stdout: str | None,
    ctx_stderr: str | None,
) -> None:
    """A `_SubprocessTimeoutError`'s captured payload is used verbatim."""
    details = _SubprocessTimeoutDetails(
        timeout=timeout,
        stdout=stdout,
        stderr=stderr,
        exited_at=exited_at,
    )
    payload = _resolve_timeout_payload(
        _SubprocessTimeoutError(details),
        # A None configured timeout would fail for a bare TimeoutError, proving
        # the carried branch never consults the fallback at all.
        _TimeoutFallback(
            configured_timeout=None,
            stdout=ctx_stdout,
            stderr=ctx_stderr,
            exited_at=injected_now,
        ),
    )
    assert payload == details


@given(
    configured_timeout=_finite_floats,
    stdout_text=_optional_text,
    stderr_text=_optional_text,
    exited_at=_finite_floats,
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
    assert payload.timeout == configured_timeout
    assert payload.exited_at == exited_at
    assert payload.stdout == stdout_text
    assert payload.stderr == stderr_text


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
    assert result == expected
    assert failure_index not in result
    assert all(not done[idx] for idx in result)
    # No double-termination scheduling: indices are unique and ordered.
    assert len(result) == len(set(result))
    assert result == sorted(result)


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
    assert _stages_to_terminate(failure_index, settled) == []
