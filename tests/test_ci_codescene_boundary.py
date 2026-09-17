"""Contracts for where CodeScene publication may happen, and what it needs.

CodeScene accepts an upload only for a branch it analyses, so publication
belongs to ``coverage-main.yml`` and the pull-request lane must not reach for
it even speculatively. Three separate controls hold that line, and each one
reads as satisfied while the others are intact:

* the pull-request lane must not invoke the CodeScene action,
* no environment scope the pull-request lane can reach may carry the access
  token, and
* the trunk publisher must ask for the upload mode rather than the
  pull-request check mode.

The first is the obvious one and the easiest to check; the other two are what
stop the first from being reintroduced, and they are the halves a review that
only reads a step list will miss.

Every contract here is a property of the workflows as shipped, so none of them
needs a live CodeScene project. Absence is asserted against the parsed
document rather than the raw text, because a token or project URL sitting in
the file is harmless while no scope hands it to a step, and only the resolved
scope says which of the two it is.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_runners import job_env, step_inputs, steps, workflow_env

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

#: The pull-request lane. It compares against the baseline main publishes and
#: must never publish, upload, or hold a credential.
PULL_REQUEST_LANE = ("ci.yml", "coverage")

#: The trunk publisher, which owns both the baseline and the CodeScene upload.
TRUNK_PUBLISHER = ("coverage-main.yml", "coverage-upload")

#: The CodeScene action's `uses:` prefix. Matched as a prefix rather than a
#: substring so a step naming a differently owned action that happens to
#: contain the phrase cannot satisfy, or trip, the contract.
CODESCENE_ACTION = "leynos/shared-actions/.github/actions/upload-codescene-coverage@"

#: The name of the secret the CodeScene action authenticates with. Not a
#: credential: it is the key an environment scope would have to declare, and
#: the contract is about that key's absence.
CODESCENE_VARIABLE = "CS_ACCESS_TOKEN"

#: The project URL the pull-request lane used to compare against before the
#: gate moved to a local ratchet. Its return would mean the check came back.
CODESCENE_PROJECT_URL = "api.codescene.io"


def _checkout(workflow_name: str, job_name: str) -> Step:
    """Return the single checkout step of a job.

    Parameters
    ----------
    workflow_name : str
        File name of the workflow.
    job_name : str
        Job whose checkout step is wanted.

    Returns
    -------
    Step
        The checkout step.
    """
    checkouts = [
        step
        for step in steps(workflow_name, job_name)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1, (
        f"{workflow_name}:{job_name} must check out exactly once, "
        f"found {len(checkouts)}"
    )
    return checkouts[0]


def _codescene_steps(workflow_name: str, job_name: str) -> list[Step]:
    """Return every step of a job that invokes the CodeScene action."""
    return [
        step
        for step in steps(workflow_name, job_name)
        if str(step.get("uses", "")).startswith(CODESCENE_ACTION)
    ]


def test_the_pull_request_lane_does_not_contact_codescene() -> None:
    """A pull request must keep its report and CodeScene token local to main.

    Three levels are checked, and each can regress without the others noticing.
    The step list catches a re-added action; the two environment scopes catch a
    token a guard could read; and the project URL catches a gate configured
    against CodeScene without the action itself named. A secret that fails to
    resolve is empty inside a step but not at the job or workflow scope, where
    an ``if: env.CS_ACCESS_TOKEN != ''`` guard would read a token and admit the
    step, and a scope the reader cannot find fails the contract rather than
    passing it: the reader can only vouch for a mapping it was handed.
    """
    workflow_name, job_name = PULL_REQUEST_LANE
    pull_request_steps = steps(workflow_name, job_name)

    assert not any(
        CODESCENE_ACTION in str(step.get("uses", "")) for step in pull_request_steps
    ), f"{workflow_name}:{job_name} must not invoke the CodeScene action"
    for scope, variables in (
        ("job", job_env(workflow_name, job_name)),
        ("workflow", workflow_env(workflow_name)),
    ):
        assert CODESCENE_VARIABLE not in variables, (
            f"{workflow_name}:{job_name} must not receive the CodeScene token "
            f"at {scope} scope"
        )
    assert CODESCENE_PROJECT_URL not in str(pull_request_steps), (
        f"{workflow_name}:{job_name} must not declare a CodeScene project"
    )


def test_the_trunk_publisher_uploads_to_codescene() -> None:
    """Main owns CodeScene publication, so it must ask for the upload mode.

    The input is load-bearing and silent when it is wrong. Dropping ``mode``
    falls back to the shared action's default, and ``check`` would switch the
    step to the pull-request comparison: either retires main-branch
    publication while every other contract in this module still passes.
    """
    workflow_name, job_name = TRUNK_PUBLISHER
    uploads = _codescene_steps(workflow_name, job_name)

    assert len(uploads) == 1, (
        f"{workflow_name}:{job_name} must upload to CodeScene exactly once, "
        f"found {len(uploads)}"
    )
    inputs = step_inputs(
        uploads[0],
        f"{workflow_name}:{job_name} CodeScene upload must declare inputs",
    )

    assert inputs.get("mode") == "upload", (
        f"{workflow_name}:{job_name} must publish with mode: upload, got "
        f"{inputs.get('mode')!r}; publication on main would stop without it"
    )


def test_the_pull_request_coverage_checkout_does_not_want_full_history() -> None:
    """Nothing in the pull-request lane reads history, so it must not fetch it.

    Full history was requested only so a CodeScene changed-line gate could
    resolve the merge base. That gate has moved to main, and a deep checkout is
    not an inert leftover: it slows every run and would let a history-reading
    gate be reintroduced without anyone noticing the depth to run it had come
    back.
    """
    workflow_name, job_name = PULL_REQUEST_LANE
    inputs = step_inputs(
        _checkout(workflow_name, job_name),
        f"{workflow_name}:{job_name} checkout must declare inputs",
    )

    assert "fetch-depth" not in inputs, (
        f"{workflow_name}:{job_name} requests fetch-depth="
        f"{inputs.get('fetch-depth')!r}; nothing in this lane reads history"
    )
