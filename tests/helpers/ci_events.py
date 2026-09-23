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

import pathlib as pth
import re
import typing as typ

from tests.helpers.ci_placement import declares_steps, placement
from tests.helpers.ci_workflows import job, jobs

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: A pull request opened from a fork of this repository.
FORK_PULL_REQUEST: typ.Final = {
    "event_name": "pull_request",
    "fork": True,
    "ref": "refs/pull/1/merge",
}
#: A pull request opened from a branch of this repository.
OWNED_PULL_REQUEST: typ.Final = {
    "event_name": "pull_request",
    "fork": False,
    "ref": "refs/pull/1/merge",
}
#: A push to the default branch. There is no pull-request payload, so the fork
#: field is absent, which GitHub evaluates as falsy.
PUSH_TO_MAIN: typ.Final = {
    "event_name": "push",
    "fork": None,
    "ref": "refs/heads/main",
}
#: A tag push, which is what reaches `release.yml`.
TAG_PUSH: typ.Final = {"event_name": "push", "fork": None, "ref": "refs/tags/v1.2.3"}

#: A comparison this evaluator can decide: one context property this module
#: models, against a quoted literal.
_COMPARISON = re.compile(
    r"\A\s*github\.(?P<field>event_name|ref)\s*(?P<operator>==|!=)\s*"
    r"'(?P<literal>[^']*)'\s*\Z"
)
#: Everything else in a condition is a run-time value this harness does not
#: model: `needs.*` outcomes, `inputs.*`, `github.actor`. Treating them as true
#: keeps the schedule a superset of what runs, which is the safe direction for
#: a placement assertion: it never claims a job is absent when it runs.
_UNKNOWN: typ.Final = True


def _decide(term: str, event: cabc.Mapping[str, object]) -> bool:
    """Evaluate one `&&`-joined term against the event."""
    match = _COMPARISON.match(term)
    if match is None:
        return _UNKNOWN
    actual = event.get(match.group("field"))
    equal = actual == match.group("literal")
    return equal if match.group("operator") == "==" else not equal


def runs_on_event(condition: object, event: cabc.Mapping[str, object]) -> bool:
    """Report whether a job's own ``if:`` admits this event.

    GitHub skips `ci.yml:coverage` on a push, because it declares
    `github.event_name == 'pull_request'`. A schedule that listed it anyway
    would say the push event places a job the push event never runs.

    Only `github.event_name` and `github.ref` comparisons are decided. Every
    other term is left to :data:`_UNKNOWN`, including any term a parenthesized
    expression splits into, since none of those match the comparison pattern
    either. An explicit parenthesis check stood here until a mutation showed it
    could not change an outcome: the unknown-term path already covers it, and
    an unreachable guard is worse than none because it reads as protection.

    This harness models the event, not `needs` outcomes or dispatch inputs, so
    a schedule that is a superset of what runs never claims a job is absent
    when it is present.

    Returns
    -------
    bool
        Whether the event admits the job.
    """
    if not isinstance(condition, str):
        return True
    text = " ".join(condition.split())
    expression = re.fullmatch(r"\$\{\{(?P<body>.*)\}\}", text, re.DOTALL)
    if expression is not None:
        text = " ".join(expression.group("body").split())
    return any(
        all(_decide(term, event) for term in alternative.split("&&"))
        for alternative in text.split("||")
    )


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
        declared = job(workflow_name, job_name)
        if not runs_on_event(declared.get("if"), event):
            continue
        if declares_steps(workflow_name, job_name):
            scheduled.append(
                Scheduled(
                    workflow_name, job_name, _select(workflow_name, job_name, event)
                )
            )
            continue
        called = declared.get("uses")
        if not isinstance(called, str) or not called.startswith("./.github/workflows/"):
            # A caller of a workflow outside this repository declares no runner
            # this harness can resolve, and its callee is not ours to model.
            continue
        # `PurePosixPath` rather than `rsplit`: a workflow reference is a POSIX
        # path and the separator is part of its grammar, not a character to
        # split on.
        scheduled.extend(schedule(pth.PurePosixPath(called).name, event))
    return scheduled
