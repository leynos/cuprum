"""Non-positive timeouts at the public boundary, for commands and pipelines.

``test_subprocess_timeout`` drives ``_wait_for_exit_code_within_timeout``
directly with process doubles, which pins the internal contract — a
``TimeoutError`` and a terminated double — but not the contract a caller sees.
These tests go through ``SafeCmd.run_sync`` and ``Pipeline.run_sync`` against
real subprocesses instead, asserting the two things the internal tests cannot:
that the internal ``TimeoutError`` surfaces as the documented
``TimeoutExpired``, and that the process actually spawned is reaped rather than
left behind.

Cleanup is checked by pid rather than by inspecting an internal process object.
``_terminate_process`` awaits ``process.wait()`` before the failure propagates,
so the child is already reaped by the time the exception reaches the caller and
``os.kill(pid, 0)`` must raise ``ProcessLookupError``.
"""

from __future__ import annotations

import os
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import ScopeConfig, TimeoutExpired, scoped, sh
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent

# Long enough that the process cannot plausibly exit on its own, so a passing
# test can only mean the timeout fired.
_SLEEP_SRC = "import time; time.sleep(30)"
_NON_POSITIVE = [0, 0.0, -0.0, -1.0, -30.0]


def _assert_reaped(pids: list[int], expected: int) -> None:
    """Assert every recorded pid was reaped and none was left running."""
    assert len(pids) == expected, (
        f"expected {expected} spawned process(es) to report a pid, got {pids!r}"
    )
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def _collect_started_pids(events: list[ExecEvent]) -> list[int]:
    """Return the pid of every subprocess that reached the ``start`` phase."""
    return [ev.pid for ev in events if ev.phase == "start" and ev.pid is not None]


def _run_command_expecting_timeout(timeout: float) -> list[ExecEvent]:
    """Run a sleeping command that must time out; return the observed events."""
    catalogue, python_program = python_catalogue()
    cmd = sh.make(python_program, catalogue=catalogue)("-c", _SLEEP_SRC)
    events: list[ExecEvent] = []

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        sh.observe(events.append),
        pytest.raises(TimeoutExpired) as exc_info,
    ):
        cmd.run_sync(timeout=timeout, output=RunOutputOptions(capture=False))

    assert exc_info.value.timeout == timeout, (
        f"TimeoutExpired must report the configured timeout {timeout!r}, got "
        f"{exc_info.value.timeout!r}"
    )
    return events


@pytest.mark.parametrize("timeout", _NON_POSITIVE)
def test_run_sync_non_positive_timeout_expires_and_reaps(timeout: float) -> None:
    """``SafeCmd.run_sync`` maps a non-positive deadline to ``TimeoutExpired``.

    The internal waiter raises a bare ``TimeoutError``; a caller is documented
    to see ``TimeoutExpired`` carrying the configured timeout. The spawned
    process must also be reaped rather than orphaned by the fast path, which
    skips the ordinary wait entirely.
    """
    events = _run_command_expecting_timeout(timeout)
    _assert_reaped(_collect_started_pids(events), 1)


@pytest.mark.parametrize("timeout", _NON_POSITIVE)
def test_pipeline_non_positive_timeout_expires_and_reaps(timeout: float) -> None:
    """``Pipeline.run_sync`` behaves the same way, for every stage.

    A pipeline enforces one deadline for the whole run, so a non-positive value
    must expire it immediately and leave no stage running.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pipeline = python("-c", _SLEEP_SRC) | python("-c", "import sys; sys.stdin.read()")
    events: list[ExecEvent] = []

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        sh.observe(events.append),
        pytest.raises(TimeoutExpired) as exc_info,
    ):
        pipeline.run_sync(timeout=timeout, output=RunOutputOptions(capture=False))

    assert exc_info.value.timeout == timeout, (
        f"TimeoutExpired must report the configured timeout {timeout!r}, got "
        f"{exc_info.value.timeout!r}"
    )
    _assert_reaped(_collect_started_pids(events), 2)


# Each example spawns and reaps a real subprocess, so the example count is kept
# low deliberately; the explicit cases above cover the boundary values and this
# generalises over the rest of the non-positive range.
@settings(max_examples=12, deadline=None)
@given(
    timeout=st.floats(
        min_value=-30.0, max_value=0.0, allow_nan=False, allow_infinity=False
    )
)
def test_any_non_positive_timeout_expires_and_reaps(timeout: float) -> None:
    """Every finite non-positive timeout expires immediately and reaps.

    The fast path keys on ``timeout <= 0`` rather than on any particular value,
    so the invariant should hold across the range and not merely at the
    hand-picked boundaries. Generating the value guards against a future
    refactor that special-cases only zero.
    """
    events = _run_command_expecting_timeout(timeout)
    _assert_reaped(_collect_started_pids(events), 1)
