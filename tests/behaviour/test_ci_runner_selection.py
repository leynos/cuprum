"""Behavioural tests for the runner each CI event selects.

The contract tests in `tests/test_ci_runner_placement.py` read one job's
declaration at a time. These scenarios evaluate the composed workflows against
an event: they resolve every expression, follow the call from `ci.yml` into
`build-wheels.yml`, and assert the runner the event actually selects.

That composition is where the interesting failures live. A lane can declare two
correct labels and still send a fork to a runner no fork can obtain, and a
called workflow's placement is invisible to any rule that reads its own
`workflow_call` trigger. `act` cannot stand in here: it skips a job whose label
it cannot map and exits zero, so the Ubicloud lanes would go unexercised while
the run reported success.
"""

from __future__ import annotations

import typing as typ

import pytest
from pytest_bdd import given, parsers, scenario, then, when

from tests.helpers.ci_events import (
    FORK_PULL_REQUEST,
    OWNED_PULL_REQUEST,
    PUSH_TO_MAIN,
    TAG_PUSH,
    schedule,
)
from tests.helpers.ci_runners import (
    FORK_REACHABLE_UBICLOUD_JOBS,
    GITHUB_LABEL,
    UBICLOUD_JOBS,
    UBICLOUD_LABEL,
    expand,
)

if typ.TYPE_CHECKING:
    from tests.helpers.ci_events import Scheduled

FEATURE = "../features/ci_runner_selection.feature"
#: The Ubicloud wheel job that only a call can reach. `build-wheels.yml` declares
#: `workflow_call` alone, so its own trigger says nothing about who runs it.
CALLED_WHEEL_JOBS: typ.Final = ("verify-wheel-install",)
PLATFORM_LABELS: typ.Final = frozenset({
    "macos-15-intel",
    "macos-latest",
    "ubuntu-latest",
    "windows-2022",
})
#: The fewest reviewed lanes any event asserting that step schedules. Named
#: rather than derived: a derivation from the same schedule the assertion
#: reads would move with the defect it is meant to catch.
MINIMUM_REVIEWED_LANES: typ.Final = 4
#: The lane whose own guard admits one event only. Its presence and absence
#: are what make the event evaluation falsifiable: every other assertion here
#: reads a label, and a wrongly scheduled job declares the right one.
PULL_REQUEST_ONLY_LANE: typ.Final = ("ci.yml", "coverage")
EVENTS: typ.Final = {
    "fork": FORK_PULL_REQUEST,
    "owned": OWNED_PULL_REQUEST,
    "push": PUSH_TO_MAIN,
}


@pytest.fixture
def selection() -> dict[str, object]:
    """Carry the workflow under test and the schedule between steps."""
    return {}


@given("the continuous integration workflow")
def _ci_workflow(selection: dict[str, object]) -> None:
    """Select the workflow every pull request and push triggers."""
    selection["workflow"] = "ci.yml"


@given("the release workflow")
def _release_workflow(selection: dict[str, object]) -> None:
    """Select the workflow a tag push triggers."""
    selection["workflow"] = "release.yml"


@when("a pull request is opened from a branch of this repository")
def _owned_pull_request(selection: dict[str, object]) -> None:
    """Resolve the schedule for a pull request from this repository."""
    selection["scheduled"] = schedule(
        typ.cast("str", selection["workflow"]), OWNED_PULL_REQUEST
    )


@when("a pull request is opened from a fork")
def _fork_pull_request(selection: dict[str, object]) -> None:
    """Resolve the schedule for a pull request from a fork."""
    selection["scheduled"] = schedule(
        typ.cast("str", selection["workflow"]), FORK_PULL_REQUEST
    )


@when("a commit is pushed to the default branch")
def _push_to_main(selection: dict[str, object]) -> None:
    """Resolve the schedule for a push to the default branch."""
    selection["scheduled"] = schedule(
        typ.cast("str", selection["workflow"]), PUSH_TO_MAIN
    )


@when("a tag is pushed")
def _tag_push(selection: dict[str, object]) -> None:
    """Resolve the schedule for a tag push."""
    selection["scheduled"] = schedule(typ.cast("str", selection["workflow"]), TAG_PUSH)


@when(parsers.parse("the {event} event is evaluated"))
def _named_event(selection: dict[str, object], event: str) -> None:
    """Resolve the schedule for the event the outline names."""
    selection["scheduled"] = schedule(
        typ.cast("str", selection["workflow"]), EVENTS[event]
    )


def _scheduled(selection: dict[str, object]) -> list[Scheduled]:
    """Return the resolved schedule, refusing an empty one.

    An empty schedule would satisfy every assertion below by vacuity, which is
    indistinguishable from a passing run until something is mutated.

    Returns
    -------
    list[Scheduled]
        Every job the event scheduled, guaranteed non-empty.
    """
    resolved = typ.cast("list[Scheduled]", selection["scheduled"])
    assert resolved, "the event scheduled no jobs, so every assertion is vacuous"
    return resolved


@then("every reviewed lane selects the Ubicloud runner")
def _reviewed_lanes_are_paid(selection: dict[str, object]) -> None:
    """Assert each reviewed lane took the Ubicloud arm.

    The `continue` below is a vacuity hole on its own: a schedule containing no
    reviewed lane at all would take it every time, check nothing, and read
    exactly like a pass. The checked count closes it.
    """
    resolved = {
        (item.workflow, item.job): item.labels for item in _scheduled(selection)
    }
    checked = 0
    for workflow_name, job_name in expand(UBICLOUD_JOBS):
        if (workflow_name, job_name) not in resolved:
            continue
        checked += 1
        assert resolved[workflow_name, job_name] == (UBICLOUD_LABEL,), (
            f"{workflow_name}:{job_name} must select {UBICLOUD_LABEL} on this "
            f"event, got {resolved[workflow_name, job_name]}"
        )
    assert checked >= MINIMUM_REVIEWED_LANES, (
        f"this event scheduled only {checked} reviewed lanes, fewer than the "
        f"{MINIMUM_REVIEWED_LANES} every event asserting this step places; the "
        "loop above would have checked nothing and still passed"
    )


@then("the wheel jobs reached through the called workflow select it too")
def _called_wheel_jobs_are_paid(selection: dict[str, object]) -> None:
    """Assert the called workflow's wheel jobs were reached and placed."""
    resolved = {item.job: item.labels for item in _scheduled(selection)}
    for job_name in CALLED_WHEEL_JOBS:
        assert job_name in resolved, (
            f"{job_name} must be reached through the call into "
            "build-wheels.yml; a caller that stopped calling it would leave "
            "every rule about its placement true and unexercised"
        )
        assert resolved[job_name] == (UBICLOUD_LABEL,), (
            f"{job_name} must select {UBICLOUD_LABEL}, got {resolved[job_name]}"
        )


@then("no lane selects a paid runner")
def _no_paid_runner_for_a_fork(selection: dict[str, object]) -> None:
    """Assert no lane asked for a runner a fork cannot obtain."""
    offenders = [
        f"{item.workflow}:{item.job}={label}"
        for item in _scheduled(selection)
        for label in item.labels
        if label.startswith("ubicloud-")
    ]
    assert not offenders, (
        "a fork's pull request cannot obtain an Ubicloud runner, so a lane "
        f"selecting one would queue until it timed out: {offenders}"
    )


@then("every fork-reachable lane selects the hosted fallback")
def _fork_lanes_fall_back(selection: dict[str, object]) -> None:
    """Assert every fork-reachable lane took the hosted arm."""
    resolved = {
        (item.workflow, item.job): item.labels for item in _scheduled(selection)
    }
    for workflow_name, job_name in expand(FORK_REACHABLE_UBICLOUD_JOBS):
        assert (workflow_name, job_name) in resolved, (
            f"{workflow_name}:{job_name} is declared fork-reachable but this "
            "event did not schedule it"
        )
        assert resolved[workflow_name, job_name] == (GITHUB_LABEL,), (
            f"{workflow_name}:{job_name} must fall back to {GITHUB_LABEL} for "
            f"a fork, got {resolved[workflow_name, job_name]}"
        )


@then("the pull-request-only lane is absent")
def _pull_request_lane_absent(selection: dict[str, object]) -> None:
    """Assert a push does not schedule the lane its own guard excludes.

    `ci.yml:coverage` declares `github.event_name == 'pull_request'`, so GitHub
    skips it on a push. A schedule that listed it anyway would report the push
    event placing a job the push event never runs, and every placement
    assertion over that schedule would still pass, because the label it
    declares is correct. Only its absence discriminates.
    """
    scheduled = {(item.workflow, item.job) for item in _scheduled(selection)}
    assert PULL_REQUEST_ONLY_LANE not in scheduled, (
        f"{PULL_REQUEST_ONLY_LANE} is guarded on the pull_request event, so a "
        "push must not schedule it"
    )


@then("the pull-request-only lane is present")
def _pull_request_lane_present(selection: dict[str, object]) -> None:
    """Prove the exclusion narrow: the event it is written for still runs it."""
    scheduled = {(item.workflow, item.job) for item in _scheduled(selection)}
    assert PULL_REQUEST_ONLY_LANE in scheduled, (
        f"{PULL_REQUEST_ONLY_LANE} must run on a pull request; excluding it "
        "here would make the absence assertion above hold for the wrong reason"
    )


@then("the native wheel matrix keeps its platform runners")
def _native_matrix_is_hosted(selection: dict[str, object]) -> None:
    """Assert the native matrix kept exactly its platform runners."""
    resolved = {item.job: item.labels for item in _scheduled(selection)}
    assert "build-native-wheels" in resolved, (
        "the native wheel matrix must be reached through the call"
    )
    assert frozenset(resolved["build-native-wheels"]) == PLATFORM_LABELS, (
        "the native matrix must keep exactly its platform runners, got "
        f"{sorted(resolved['build-native-wheels'])}"
    )


@then("every selected label is a concrete runner name")
def _labels_are_schedulable(selection: dict[str, object]) -> None:
    """Assert every selected label is one GitHub could schedule."""
    for item in _scheduled(selection):
        for label in item.labels:
            assert "${{" not in label, (
                f"{item.workflow}:{item.job} selected an unevaluated "
                f"expression: {label!r}"
            )
            assert label, f"{item.workflow}:{item.job} selected an empty label"
            assert label == label.strip(), (
                f"{item.workflow}:{item.job} selected {label!r}; GitHub keeps "
                "whatever surrounds an interpolation, and a padded label "
                "matches no runner"
            )


@scenario(FEATURE, "an owned pull request keeps every reviewed lane on the paid runner")
def test_an_owned_pull_request_keeps_the_paid_lane() -> None:
    """An internal pull request uses the runners the repository pays for."""


@scenario(FEATURE, "a fork's pull request selects only runners a fork can obtain")
def test_a_fork_pull_request_is_schedulable() -> None:
    """A fork's pull request never waits on a runner it cannot be given."""


@scenario(FEATURE, "a push to the default branch takes the owned arm")
def test_a_push_takes_the_owned_arm() -> None:
    """With no pull-request payload the fork field is absent, so falsy."""


@scenario(FEATURE, "a push does not schedule the pull-request-only lane")
def test_a_push_skips_the_pull_request_only_lane() -> None:
    """A job's own event guard is honoured by the schedule."""


@scenario(FEATURE, "a pull request does schedule the pull-request-only lane")
def test_a_pull_request_runs_the_pull_request_only_lane() -> None:
    """The same guard admits the event it is written for."""


@scenario(FEATURE, "a tag push reaches the wheel jobs on the paid runner")
def test_a_tag_push_reaches_the_wheel_jobs() -> None:
    """The release path resolves through the same called workflow."""


@scenario(FEATURE, "every selected runner is a schedulable label")
def test_every_selected_runner_is_schedulable() -> None:
    """No event may select an unevaluated or padded label."""
