"""Behavioural tests for opt-in process-group cleanup.

The scenarios all start the same shape — a run whose direct child leaves a
``SIGTERM``-immune grandchild holding the run's pipes — and differ only in the
ownership policy and how often the run is cancelled. The step bodies below
carry the assertions; the scaffolding that starts and cancels the run lives in
``_process_group_cleanup_support``.

The policy is the whole subject, so the two policies are asserted against each
other. An owned run must reclaim the descendant and a process outside its group
must survive; an inheriting run must leave the descendant alone. Without the
inheriting scenario, a bug that contained descendants unconditionally would
pass the owned one, and without the owned scenario the containment claim would
be untested.
"""

from __future__ import annotations

import sys
import typing as typ

import pytest
from pytest_bdd import given, parsers, scenario, then, when

from tests.behaviour._process_group_cleanup_support import (
    SETTLE_BOUND_S,
    CleanupOutcome,
    policy_for,
    run_scenario,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import ProcessGroupPolicy

# Process-group ownership is a POSIX guarantee. Windows has no equivalent
# primitive and the policy is rejected there rather than emulated, so the
# scenarios are scoped to the platform that has it.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

# The outcomes one scenario produced, keyed by the request that ran it. Steps
# are handed the same fixture namespace, so a scenario's setup and its
# assertions meet here rather than through a module global.
_OUTCOMES_KEY = "cleanup_outcomes"


@scenario(
    "../features/process_group_cleanup.feature",
    "An owned run reclaims a grandchild that ignores termination",
)
def test_owned_run_reclaims_the_grandchild() -> None:
    """Behavioural coverage for owned-group descendant containment."""


@scenario(
    "../features/process_group_cleanup.feature",
    "An inheriting run leaves its grandchild alone",
)
def test_inheriting_run_leaves_the_grandchild() -> None:
    """Behavioural coverage for the inherited default's bounded reach."""


@scenario(
    "../features/process_group_cleanup.feature",
    "A repeated cancellation does not abandon the cleanup",
)
def test_repeated_cancellation_still_cleans_up() -> None:
    """Behavioural coverage for cancellation-safe owned teardown."""


class _CleanupRequest(typ.TypedDict):
    """The policy and cancellation count one scenario asks for."""

    policy: typ.NotRequired[ProcessGroupPolicy]
    cancellations: typ.NotRequired[int]


@pytest.fixture
def cleanup_request() -> _CleanupRequest:
    """Return the mutable request the Given and When steps fill in."""
    return {}


@pytest.fixture
def cleanup_outcomes() -> dict[str, CleanupOutcome]:
    """Return the store the When step records outcomes in."""
    return {}


@given(
    "a command whose child leaves a SIGTERM-immune grandchild",
    target_fixture="cleanup_request",
)
def given_pipe_holding_command() -> _CleanupRequest:
    """Declare the run shape every scenario in this feature shares."""
    return {}


@given(parsers.parse("the command is run under the {policy} process-group policy"))
def given_run_under_policy(
    cleanup_request: _CleanupRequest,
    policy: str,
) -> None:
    """Record the ownership policy the scenario runs under."""
    cleanup_request["policy"] = policy_for(policy)


@when("the run is cancelled")
def when_run_is_cancelled(
    cleanup_request: _CleanupRequest,
    cleanup_outcomes: dict[str, CleanupOutcome],
    tmp_path: Path,
) -> None:
    """Run the scenario once, cancelling the run a single time."""
    cleanup_outcomes["latest"] = run_scenario(
        tmp_path,
        cleanup_request["policy"],
        cancellations=1,
    )


@when("the run is cancelled twice")
def when_run_is_cancelled_twice(
    cleanup_request: _CleanupRequest,
    cleanup_outcomes: dict[str, CleanupOutcome],
    tmp_path: Path,
) -> None:
    """Run the scenario once, cancelling the run twice while it settles."""
    cleanup_outcomes["latest"] = run_scenario(
        tmp_path,
        cleanup_request["policy"],
        cancellations=2,
    )


def _latest(cleanup_outcomes: dict[str, CleanupOutcome]) -> CleanupOutcome:
    """Return the outcome the When step recorded."""
    assert "latest" in cleanup_outcomes, "the run must have been cancelled first"
    return cleanup_outcomes["latest"]


@then("the grandchild is gone after the run settles")
def then_grandchild_is_gone(cleanup_outcomes: dict[str, CleanupOutcome]) -> None:
    """Assert the owned teardown reached the descendant holding the pipes.

    The settling is asserted before the reach, because a run that never settled
    has no "after" for the rest to describe — and a timeout reported as a
    leftover process would name the wrong defect.
    """
    outcome = _latest(cleanup_outcomes)
    assert not outcome.rescued, (
        "the run never settled on its own: its teardown had to be unblocked "
        "by killing the descendant it could not reach, so it reclaimed nothing"
    )
    assert outcome.raised == "CancelledError", (
        f"the cancelled run must settle by propagating the cancellation, "
        f"not by {outcome.raised}"
    )
    assert outcome.settle_seconds < SETTLE_BOUND_S, (
        f"the run must settle within its documented bounds; took "
        f"{outcome.settle_seconds:.2f}s"
    )
    assert not outcome.grandchild_alive, (
        "an owned run must reclaim the descendant its direct child left behind; "
        f"pid {outcome.grandchild_pid} was still running once it settled"
    )
    assert not outcome.direct_child_alive, (
        f"the run's own child (pid {outcome.direct_child_pid}) must be reaped"
    )


@then("the run's direct child is gone")
def then_direct_child_is_gone(cleanup_outcomes: dict[str, CleanupOutcome]) -> None:
    """Assert the inheriting teardown still ended the direct child."""
    outcome = _latest(cleanup_outcomes)
    assert outcome.raised == "CancelledError", (
        f"the cancelled run must settle by propagating the cancellation, "
        f"not by {outcome.raised}"
    )
    assert outcome.settle_seconds < SETTLE_BOUND_S, (
        f"the run must settle within its documented bounds; took "
        f"{outcome.settle_seconds:.2f}s"
    )
    assert not outcome.direct_child_alive, (
        f"the inherited policy must still terminate and reap the direct child "
        f"(pid {outcome.direct_child_pid})"
    )


@then("the grandchild is still running")
def then_grandchild_survives(cleanup_outcomes: dict[str, CleanupOutcome]) -> None:
    """Assert an inheriting run left the descendant alone.

    This is the control for the owned scenario: if the teardown signalled a
    group it does not own, the grandchild would be gone too, and the scenario
    would pass for the wrong reason.
    """
    outcome = _latest(cleanup_outcomes)
    assert outcome.grandchild_alive, (
        "an inheriting run must not signal a group it does not own, so the "
        f"descendant (pid {outcome.grandchild_pid}) must outlive it"
    )


@then("the run's streams have settled")
def then_streams_settled(cleanup_outcomes: dict[str, CleanupOutcome]) -> None:
    """Assert the run left no stream reader waiting on a held pipe."""
    outcome = _latest(cleanup_outcomes)
    assert outcome.pending == 0, (
        f"a settled run must leave no task behind; found {outcome.pending}"
    )
    assert outcome.settle_seconds < SETTLE_BOUND_S, (
        "the run must settle within its documented bounds rather than waiting "
        f"on a pipe a descendant still holds; took {outcome.settle_seconds:.2f}s"
    )


@then("a process outside the run's group is untouched")
def then_unrelated_untouched(cleanup_outcomes: dict[str, CleanupOutcome]) -> None:
    """Assert the owned teardown did not reach beyond the group it created."""
    outcome = _latest(cleanup_outcomes)
    assert outcome.unrelated_alive, (
        "an owned teardown must signal only the group it created; the "
        f"unrelated process (pid {outcome.unrelated_pid}) was gone afterwards"
    )
