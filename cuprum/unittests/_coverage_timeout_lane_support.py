"""Read coverage-lane timers from GitHub Actions workflows.

The timeout-ordering contract uses these helpers to resolve a coverage action's
watchdogs, job ceiling, conditions, and manifest inputs without obscuring its
assertions with YAML traversal. Call :func:`_lanes` for the live lanes or
:func:`lanes_in` with a synthetic workflow to exercise the reader.

This module owns the workflow shapes and the readers that walk them; the
nextest tiers live in ``cuprum.unittests._timeout_lane_support``. The two
halves read different files, and only the contract brings them together, so
this module depends on that one for the shared constants and not the reverse.
"""

from __future__ import annotations

import functools
import typing as typ

import yaml

from cuprum.unittests._timeout_lane_support import (
    CEILING_MARGIN_SECONDS,
    COVERAGE_ACTION,
    COVERAGE_WORKFLOWS,
    OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS,
    WATCHDOG_VARIABLE,
)
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def required_ceiling(budgets: cabc.Sequence[int]) -> int:
    """Return the smallest acceptable ceiling for one job, in seconds.

    Three terms. Each coverage step may legitimately spend its whole
    watchdog, so their sum is the floor. Measured work outside those
    windows and a margin prevent the job timer pre-empting a diagnosis.

    Parameters
    ----------
    budgets : cabc.Sequence[int]
        One watchdog budget per coverage step in the job.

    Returns
    -------
    int
        The smallest acceptable ceiling, in seconds.
    """
    return sum(budgets) + OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS + CEILING_MARGIN_SECONDS


class Step(typ.TypedDict, total=False):
    """One workflow step, declaring only the fields this contract reads.

    Attributes
    ----------
    name : object
        The declared step name used to locate a failure.
    uses : object
        The action the step invokes.
    env : dict[str, object]
        The innermost environment that can set the cargo watchdog.

    The ``if`` and ``with`` fields are accessed through casts because ``if`` is
    a keyword and both fields remain optional in arbitrary workflow steps.
    """

    name: object
    uses: object
    env: dict[str, object]


class Job(typ.TypedDict, total=False):
    """One workflow job, declaring only the fields this contract reads.

    Attributes
    ----------
    steps : list[Step]
        The job's steps in execution order.
    env : dict[str, object]
        The job-level watchdog environment, used when its steps inherit it.
    """

    steps: list[Step]
    env: dict[str, object]


class Workflow(typ.TypedDict, total=False):
    """A parsed workflow, declaring only the fields this contract reads.

    Attributes
    ----------
    jobs : dict[str, Job]
        Jobs keyed by their workflow identifier.
    env : dict[str, object]
        The outermost watchdog environment.
    """

    jobs: dict[str, Job]
    env: dict[str, object]


#: The condition each coverage lane legitimately carries, keyed by
#: workflow path and job, as the step's ``if`` and its job's.
#:
#: A skipped step runs no `cargo`, so its watchdog never arms and every
#: assertion below says nothing about it. `if: false` on either would
#: leave a lane that looks bounded and is not. The values are pinned
#: rather than merely tolerated, because a lane gaining, losing or
#: changing a condition changes when it runs at all.
#:
#: `ci.yml`'s coverage job runs on pull requests only; the trunk lane
#: covers pushes and carries no condition.
class CoverageLane(typ.NamedTuple):
    """One coverage job's per-step watchdogs, ceiling, and conditions.

    Attributes
    ----------
    workflow : str
        The workflow file path.
    job : str
        The job identifier.
    watchdogs : tuple[int | None, ...]
        Resolved watchdogs, one for each coverage-action invocation.
    ceiling : int or None
        The job's ``timeout-minutes`` value.
    conditions : tuple[tuple[object, object], ...]
        Each coverage step's and enclosing job's conditions.

    Watchdogs remain a tuple because separate action invocations can carry
    different budgets.
    """

    workflow: str
    job: str
    watchdogs: tuple[int | None, ...]
    ceiling: int | None
    conditions: tuple[tuple[object, object], ...] = ()

    def __str__(self) -> str:
        """Return a location suitable for a failure message.

        Returns
        -------
        str
            ``workflow:job`` for this lane.
        """
        return f"{self.workflow}:{self.job}"


@functools.cache
def _workflow(path: str) -> Workflow:
    """Parse one workflow file."""
    parsed = yaml.safe_load((repo_root() / path).read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path} must parse to a mapping"
    return typ.cast("Workflow", parsed)


def _watchdog_of(workflow: Workflow, job: Job, step: Step) -> int | None:
    """Return the watchdog budget in force for one step.

    All three levels are read, innermost first, as GitHub resolves them.
    Reading only one of them would report a lane that sets the value
    elsewhere as inheriting the action's default, which is the opposite
    of what this contract is for.

    Parameters
    ----------
    workflow : Workflow
        The whole workflow document.
    job : Job
        The enclosing job.
    step : Step
        The coverage step.

    Returns
    -------
    int or None
        The budget in seconds, or None when no level sets one.
    """
    for source in (step.get("env"), job.get("env"), workflow.get("env")):
        if not isinstance(source, dict):
            continue
        raw = source.get(WATCHDOG_VARIABLE)
        if raw is not None:
            return int(str(raw))
    return None


def _lanes() -> tuple[CoverageLane, ...]:
    """Return every job invoking the coverage action, with its budgets."""
    found: list[CoverageLane] = []
    for path in COVERAGE_WORKFLOWS:
        workflow = _workflow(path)
        jobs = workflow.get("jobs")
        assert isinstance(jobs, dict), f"{path} must declare a jobs mapping"
        found.extend(lanes_in(path, workflow))
    return tuple(found)


def lanes_in(path: str, workflow: Workflow) -> list[CoverageLane]:
    """Return one lane per coverage-invoking job in one workflow.

    Parameters
    ----------
    path : str
        The workflow path for failure messages.
    workflow : Workflow
        The parsed workflow document.

    Returns
    -------
    list[CoverageLane]
        Coverage jobs with their resolved watchdogs and ceiling.
    """
    found: list[CoverageLane] = []
    jobs = workflow.get("jobs")
    if isinstance(jobs, dict):
        for name, job in jobs.items():
            steps = [
                step
                for step in (job.get("steps") or [])
                if COVERAGE_ACTION in str(step.get("uses", ""))
            ]
            if not steps:
                continue
            raw_ceiling = typ.cast("dict[str, object]", job).get("timeout-minutes")
            found.append(
                CoverageLane(
                    workflow=path,
                    job=str(name),
                    watchdogs=tuple(
                        _watchdog_of(workflow, job, step) for step in steps
                    ),
                    ceiling=None if raw_ceiling is None else int(str(raw_ceiling)),
                    conditions=tuple(
                        (
                            typ.cast("dict[str, object]", step).get("if"),
                            typ.cast("dict[str, object]", job).get("if"),
                        )
                        for step in steps
                    ),
                )
            )
    return found


def _jobs_of(workflow: Workflow) -> list[tuple[str, Job]]:
    """Return one workflow's jobs, or nothing when it declares none."""
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return []
    return [(str(name), job) for name, job in jobs.items()]


def _coverage_steps() -> list[tuple[str, str, int, Step]]:
    """Return every step that invokes the coverage action."""
    return [
        (path, name, index + 1, step)
        for path in COVERAGE_WORKFLOWS
        for name, job in _jobs_of(_workflow(path))
        for index, step in enumerate(job.get("steps") or [])
        if COVERAGE_ACTION in str(step.get("uses", ""))
    ]


def _cargo_manifest_of(step: Step) -> object:
    """Return the ``cargo-manifest`` input a step passes, or None."""
    inputs = typ.cast("dict[str, object]", step).get("with")
    return inputs.get("cargo-manifest") if isinstance(inputs, dict) else None
