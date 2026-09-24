"""Run the leg flag of `typecheck-test` under `act` and assert which steps ran.

`tests/test_ci_experimental_leg.py` holds the flag and the guards as text. These
scenarios execute them: each projects the checked-in job, with its step bodies
replaced by probes, into a throwaway repository and runs one matrix leg for one
event. The runner's own expression engine then decides every step, so a flag
that rendered as something other than the string the guards compare against
would show here as a leg that skipped work it must do, or did work it must not.

Like the other scenarios, these need `act` and are opt-in (`make test-act`);
`CUPRUM_REQUIRE_ACT=1` turns a missing runtime from a skip into a failure. They
run in `act`'s host mode, so they pull no image.
"""

from __future__ import annotations

import json
import pathlib as pth
import typing as typ

import pytest

from tests.helpers.act_event import Event, EventName
from tests.helpers.act_harness import (
    DEFAULT_BRANCH,
    commit_paths,
    harness_skip_reason,
)
from tests.helpers.act_leg_gate import ran_steps, run_leg, stage_leg_probe
from tests.helpers.act_runtime import git, git_commit

#: The repository under test, whose `typecheck-test` is projected.
WORKTREE = pth.Path(__file__).resolve().parents[2]
#: A host-mode leg takes seconds; the bound covers a slow `act` start.
SCENARIO_TIMEOUT = 180
#: The branch a pull request comes from.
FEATURE_BRANCH = "feature"
#: The experimental leg, and a required one to compare it with.
EXPERIMENTAL = "3.15a"
REQUIRED = "3.12"
#: Work every leg that runs must do. Named, so a leg that ran only its
#: bookkeeping steps cannot pass.
WORK: typ.Final = frozenset({"Check out repository", "Run typechecker", "Run tests"})
#: The saves that only a push to `main` may run.
SAVES: typ.Final = frozenset({"Save the installed tools", "Save the compiler cache"})


@pytest.fixture
def scenario(tmp_path: pth.Path) -> pth.Path:
    """Return a repository holding the projected job, on its base commit.

    Returns
    -------
    pathlib.Path
        The scenario repository, checked out on the default branch.
    """
    reason = harness_skip_reason()
    if reason:
        pytest.skip(reason)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", f"--initial-branch={DEFAULT_BRANCH}", "--quiet")
    stage_leg_probe(root, WORKTREE)
    git(root, "add", "--all")
    git_commit(root, message="stage the projected job")
    return root


def _event_fixture(name: str) -> dict[str, object]:
    """Load the harness's minimal webhook template for ``name``."""
    path = WORKTREE / "tests" / "fixtures" / "events" / f"{name}-empty.event.json"
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), "event fixture must be a JSON mapping"
    return typ.cast("dict[str, object]", payload)


def _pull_request(repository: pth.Path) -> Event:
    """Return a pull request from a feature branch of ``repository``."""
    git(repository, "checkout", "--quiet", "-b", FEATURE_BRANCH)
    head = commit_paths(repository, [], message="scenario change")
    return Event(
        name=EventName.PULL_REQUEST,
        payload=_event_fixture("pull_request"),
        ref="refs/pull/1/merge",
        sha=head,
        branch=FEATURE_BRANCH,
    )


def _push(repository: pth.Path) -> Event:
    """Return a push of one new commit to the default branch."""
    base = git(repository, "rev-parse", "HEAD").strip()
    head = commit_paths(repository, [], message="scenario change")
    return Event(
        name=EventName.PUSH,
        payload={**_event_fixture("push"), "before": base, "after": head},
        ref=f"refs/heads/{DEFAULT_BRANCH}",
        sha=head,
        branch=DEFAULT_BRANCH,
    )


@pytest.mark.timeout(SCENARIO_TIMEOUT)
def test_the_experimental_leg_runs_no_step_on_a_pull_request(
    scenario: pth.Path,
) -> None:
    """The leg starts, skips every step of its own, and still succeeds."""
    run = run_leg(scenario, _pull_request(scenario), EXPERIMENTAL)
    assert run.exit_code == 0, run.failure_context()
    assert ran_steps(run) == set(), (
        f"the {EXPERIMENTAL} leg must run nothing on a pull request, ran "
        f"{sorted(ran_steps(run))}"
    )


@pytest.mark.timeout(SCENARIO_TIMEOUT)
def test_a_required_leg_does_its_work_on_a_pull_request(scenario: pth.Path) -> None:
    """The flag is true for every other leg, so their guards read as before."""
    run = run_leg(scenario, _pull_request(scenario), REQUIRED)
    assert run.exit_code == 0, run.failure_context()
    ran = ran_steps(run)
    assert ran >= WORK, f"the {REQUIRED} leg must run {sorted(WORK - ran)}"
    assert not SAVES & ran, f"a pull request must not save: {sorted(SAVES & ran)}"


@pytest.mark.timeout(SCENARIO_TIMEOUT)
@pytest.mark.parametrize("label", [EXPERIMENTAL, REQUIRED])
def test_every_leg_does_its_work_and_saves_on_a_push(
    scenario: pth.Path, label: str
) -> None:
    """On a push to `main` the experimental leg writes its family like the rest."""
    run = run_leg(scenario, _push(scenario), label)
    assert run.exit_code == 0, run.failure_context()
    ran = ran_steps(run)
    assert ran >= WORK | SAVES, (
        f"the {label} leg must run {sorted((WORK | SAVES) - ran)} on a push"
    )
