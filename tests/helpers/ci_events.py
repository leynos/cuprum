"""Resolve the runner each workflow event actually selects.

The placement contracts read declarations. This module evaluates them: given an
event, it resolves every job's `runs-on` to the concrete label GitHub would
schedule on, following a reusable-workflow call into its callee, so the
`ci.yml` to `build-wheels.yml` path is exercised rather than asserted a job at
a time.

`act` is not the tool for this. It skips a job whose label it cannot map and
exits zero, so an Ubicloud lane would be silently unexercised and the run would
report success: a harness that cannot fail on the thing it exists to check.
Evaluating the expression against an event payload is the part that can be
modelled honestly without a runner, and it is the part that decides whether a
fork's pull request can be scheduled at all.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_placement import declares_steps, placement
from tests.helpers.ci_workflows import job, jobs

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: A pull request opened from a fork of this repository.
FORK_PULL_REQUEST: typ.Final = {"event_name": "pull_request", "fork": True}
#: A pull request opened from a branch of this repository.
OWNED_PULL_REQUEST: typ.Final = {"event_name": "pull_request", "fork": False}
#: A push to the default branch. There is no pull-request payload, so the fork
#: field is absent, which GitHub evaluates as falsy.
PUSH_TO_MAIN: typ.Final = {"event_name": "push", "fork": None}
#: A tag push, which is what reaches `release.yml`.
TAG_PUSH: typ.Final = {"event_name": "push", "fork": None}


class Scheduled(typ.NamedTuple):
    """One job as an event would schedule it.

    Attributes
    ----------
    workflow:
        The workflow the job is declared in, which is the callee for a job
        reached through a reusable-workflow call.
    job:
        The job key within that workflow.
    labels:
        The label or labels the event selects. A matrix job selects one per
        leg; every other job selects exactly one.
    """

    workflow: str
    job: str
    labels: tuple[str, ...]


def _select(
    workflow_name: str, job_name: str, event: cabc.Mapping[str, object]
) -> tuple[str, ...]:
    """Return the labels one event selects for one job."""
    placed = placement(workflow_name, job_name)
    if placed.kind == "matrix":
        return tuple(sorted(placed.labels))
    if placed.kind == "literal":
        return (typ.cast("str", placed.owned),)
    # The fork fallback. GitHub evaluates the condition against the event
    # payload: absent on a push, false on an owned pull request, true on a
    # fork's. Only a truthy value takes the hosted arm.
    takes_fork_arm = bool(event.get("fork"))
    return (typ.cast("str", placed.fork if takes_fork_arm else placed.owned),)


def schedule(workflow_name: str, event: cabc.Mapping[str, object]) -> list[Scheduled]:
    """Resolve every job one event schedules, following reusable-workflow calls.

    Parameters
    ----------
    workflow_name:
        The workflow the event triggers.
    event:
        One of the event constants in this module.

    Returns
    -------
    list[Scheduled]
        Every job the event would schedule, with a called workflow's jobs
        reported under the callee's name rather than the caller's, because that
        is where their runners are declared.

    Notes
    -----
    A called workflow inherits its caller's `github` context, so the same event
    is passed through the call unchanged. That inheritance is the reason the
    fork expression works inside `build-wheels.yml` without an extra input, and
    modelling the call is what makes that claim testable.
    """
    scheduled: list[Scheduled] = []
    for job_name in jobs(workflow_name):
        if declares_steps(workflow_name, job_name):
            scheduled.append(
                Scheduled(
                    workflow_name, job_name, _select(workflow_name, job_name, event)
                )
            )
            continue
        called = job(workflow_name, job_name).get("uses")
        if not isinstance(called, str) or not called.startswith("./.github/workflows/"):
            # A caller of a workflow outside this repository declares no runner
            # this harness can resolve, and its callee is not ours to model.
            continue
        scheduled.extend(schedule(called.rsplit("/", 1)[-1], event))
    return scheduled
