"""Contracts for which runs may publish the coverage ratchet baseline.

A ratchet is only a ratchet while the baseline it compares against comes from
somewhere a pull request cannot reach. Before the pinned revision the shared
action published from every run that reached its save step, so a pull request
advanced the baseline it was then measured against, and a dispatch gathering
warm-cache evidence replaced the generation it was measuring.

None of that is visible from a green run. A ratchet comparing each pull request
against itself passes exactly as one comparing against trunk does, and it goes
on passing while coverage falls.

Two facts have to hold together, and neither is sufficient alone. The action
must be pinned at or beyond the revision that added the guard, which
``GENERATE_COVERAGE`` asserts by value. And this repository's merges land
through the automerge workflow's ``GITHUB_TOKEN``, which fires no push event,
so its trunk publisher has to opt back in explicitly or the baseline stops
advancing altogether.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.ci_runners import GENERATE_COVERAGE, step_inputs, steps

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

#: The workflow and job that publishes the trunk baseline, and the mode it
#: must ask for. ``always`` is not a preference here: see the module docstring.
TRUNK_PUBLISHER = ("coverage-main.yml", "coverage-upload")

#: The pull-request lane, which must never publish whatever the run's coverage
#: turned out to be.
PULL_REQUEST_LANE = ("ci.yml", "coverage")


def _coverage_step(workflow_name: str, job_name: str) -> Step:
    """Return the single shared-coverage step of a job."""
    matches = [
        step
        for step in steps(workflow_name, job_name)
        if step.get("uses") == GENERATE_COVERAGE
    ]
    assert len(matches) == 1, (
        f"{workflow_name}:{job_name} must invoke the coverage action exactly "
        f"once, found {len(matches)}"
    )
    return matches[0]


def test_the_trunk_publisher_opts_in_to_publishing() -> None:
    """The workflow carrying an automerged change must publish its baseline.

    Its merges fire no push event, so under the action's default guard the
    dispatch that uploads an automerged change's coverage would publish
    nothing, and the baseline every pull request is measured against would stop
    advancing.
    """
    workflow_name, job_name = TRUNK_PUBLISHER
    inputs = step_inputs(
        _coverage_step(workflow_name, job_name),
        f"{workflow_name}:{job_name} coverage must declare inputs",
    )

    assert inputs.get("publish-baseline") == "always", (
        f"{workflow_name}:{job_name} must set publish-baseline: always, got "
        f"{inputs.get('publish-baseline')!r}; without it an automerged change "
        f"never advances the baseline"
    )


def test_the_pull_request_lane_does_not_opt_in() -> None:
    """A pull request must not publish the baseline it is measured against.

    Leaving the input unset is what keeps it out: the action then publishes
    only on a push to ``refs/heads/main``.
    """
    workflow_name, job_name = PULL_REQUEST_LANE
    inputs = step_inputs(
        _coverage_step(workflow_name, job_name),
        f"{workflow_name}:{job_name} coverage must declare inputs",
    )

    assert "publish-baseline" not in inputs, (
        f"{workflow_name}:{job_name} sets publish-baseline="
        f"{inputs.get('publish-baseline')!r}; a pull request would then "
        f"advance the baseline it is measured against"
    )


@pytest.mark.parametrize(
    ("workflow_name", "job_name"), [TRUNK_PUBLISHER, PULL_REQUEST_LANE]
)
def test_the_ratchet_is_enabled_where_publication_is_decided(
    workflow_name: str, job_name: str
) -> None:
    """Both lanes must run the ratchet, or the guard governs nothing."""
    inputs = step_inputs(
        _coverage_step(workflow_name, job_name),
        f"{workflow_name}:{job_name} coverage must declare inputs",
    )

    assert inputs.get("with-ratchet") == "true", (
        f"{workflow_name}:{job_name} must enable the ratchet, got "
        f"{inputs.get('with-ratchet')!r}"
    )
