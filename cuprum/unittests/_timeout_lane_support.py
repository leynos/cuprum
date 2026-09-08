"""Reads the coverage lanes and their budgets out of the workflows.

Separated from ``test_timeout_ordering_contract`` so the workflow
reading and the assertions stay legible apart, and so neither module
outgrows the 400-line limit ``AGENTS.md`` sets.
"""

from __future__ import annotations

import functools
import typing as typ

import yaml

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The workflows carrying a coverage lane. Both are read, so a lane that
#: gained a budget in one and lost it in the other cannot pass by being
#: half right.
COVERAGE_WORKFLOWS: typ.Final[tuple[str, ...]] = (
    ".github/workflows/ci.yml",
    ".github/workflows/coverage-main.yml",
)

#: The environment variable the shared coverage action reads for its
#: wall-clock cap on one `cargo` invocation.
WATCHDOG_VARIABLE: typ.Final[str] = "RUN_RUST_CARGO_WAIT_TIMEOUT"

#: The action whose steps run under that watchdog.
COVERAGE_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/generate-coverage"
)

#: The budget those lanes must carry. Asserted by value rather than
#: merely as present, because the value equals nothing memorable and a
#: silent drift back towards the action's default would be invisible.
#:
#: Sized from run history rather than guessed. The coverage step has
#: never exceeded 418 s, on run 34071469378, read across roughly fifty
#: successful runs of both workflows; the trunk lane's worst was 322 s on
#: run 34062626757. None of those was a genuinely cold compile.
#: rstest-bdd's cold run took about four times its warm one, which would
#: put this repository at the old 1,800 s default's shoulder, so the
#: budget is six times the worst seen and a cold first run on a branch
#: finishes inside it.
EXPECTED_WATCHDOG_SECONDS: typ.Final[int] = 2700

#: Everything in a coverage job that is not the `cargo` invocation the
#: watchdog bounds. The job timer covers it; the watchdog does not.
#:
#: Measured per lane from the worst of several runs rather than one: 43 s
#: on run 34071469378 for the pull-request lane and 51 s on run
#: 34067223641 for the trunk lane, across twelve successful runs of each.
#: Five minutes is six times the worse of those, matching the margin the
#: watchdog itself carries.
OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS: typ.Final[int] = 5 * 60

#: How far a ceiling must sit above the sum it contains, rather than
#: merely reaching it. A ceiling equal to that sum cancels the job at
#: the moment the watchdog would have reported the overrun, and the
#: report is the only thing that makes an overrun actionable.
CEILING_MARGIN_SECONDS: typ.Final[int] = 15 * 60


class Step(typ.TypedDict, total=False):
    """One step of a job, declaring only the keys these tests read.

    Every key is optional: a step that neither uses an action nor sets
    an environment declares none of them.

    Attributes
    ----------
    name : object
        The step's declared name, used to locate it in a failure.
    uses : object
        The action the step invokes, when it invokes one.
    env : dict[str, object]
        The step-level environment, the innermost scope the watchdog is
        resolved from.
    with : dict[str, object]
        The step's inputs, read for the coverage action's
        ``cargo-manifest``.

    The step's ``if`` is read through a cast rather than declared here:
    ``if`` is a keyword, so a class-syntax TypedDict cannot carry it as
    a field.
    """

    name: object
    uses: object
    env: dict[str, object]


class Job(typ.TypedDict, total=False):
    """One job of a workflow, declaring only the keys these tests read.

    Attributes
    ----------
    steps : list[Step]
        The job's steps, in the order it runs them.
    env : dict[str, object]
        The job-level environment, consulted for the watchdog when the
        step names none.

    The job's ``if`` is read through a cast, as on :class:`Step`.
    """

    steps: list[Step]
    env: dict[str, object]


class Workflow(typ.TypedDict, total=False):
    """A parsed workflow file, declaring only the keys these tests read.

    Attributes
    ----------
    jobs : dict[str, Job]
        The workflow's jobs, keyed by identifier.
    env : dict[str, object]
        The workflow-level environment, the outermost scope the watchdog
        is resolved from.
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
    """One job that invokes the coverage action, with its budgets.

    Attributes
    ----------
    workflow : str
        The workflow file's path.
    job : str
        The job's identifier.
    watchdogs : tuple[int | None, ...]
        The budget in force for each coverage step the job runs, in
        order, or None for a step where no level sets one and it
        inherits the action's default.

        A tuple rather than one value and a count, because the budgets
        need not agree: the variable resolves per step, so a job running
        the action twice can raise it for the feature set that builds
        more. Multiplying one step's budget by the number of steps
        describes such a job only when they happen to match.
    ceiling : int or None
        The job's ``timeout-minutes``, or None when it declares none.
    conditions : tuple[tuple[object, object], ...]
        The ``if`` on each coverage step and on its job, in step order.
        A skipped step runs no ``cargo``, so its watchdog never arms and
        every budget above says nothing about it.
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
    """Parse one workflow file.

    Parameters
    ----------
    path : str
        The workflow's path below the repository root.

    Returns
    -------
    Workflow
        The parsed document.
    """
    parsed = yaml.safe_load((repo_root() / path).read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path} must parse to a mapping"
    return typ.cast("Workflow", parsed)


def required_ceiling(budgets: cabc.Sequence[int]) -> int:
    """Return the smallest acceptable ceiling for one job, in seconds.

    Three terms. Each coverage step may legitimately spend its whole
    watchdog, so the sum is the floor, and it is a sum rather than a
    multiple because the budgets need not agree. The measured work
    outside those windows is added because the job timer covers it and
    the watchdogs do not. The margin is added because a ceiling equal to
    that sum cancels the job at the moment the watchdog would have
    reported the overrun.

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
    """Return every job invoking the coverage action, with its budgets.

    Jobs are the unit rather than steps, because the ceiling belongs to a
    job and has to contain every watchdog inside it. Counting the steps
    is what would make a second invocation visible to the arithmetic.

    Returns
    -------
    tuple[CoverageLane, ...]
        One entry per coverage-invoking job.
    """
    found: list[CoverageLane] = []
    for path in COVERAGE_WORKFLOWS:
        workflow = _workflow(path)
        jobs = workflow.get("jobs")
        assert isinstance(jobs, dict), f"{path} must declare a jobs mapping"
        found.extend(lanes_in(path, workflow))
    return tuple(found)


def lanes_in(path: str, workflow: Workflow) -> list[CoverageLane]:
    """Return one lane per coverage-invoking job in one workflow.

    Separated from :func:`_lanes` so the reading can be driven with a
    workflow written for a case. Both lanes in this repository run the
    action once, so a reading that took the first step's budget and
    repeated it would agree with a correct one against the tree.

    Parameters
    ----------
    path : str
        The workflow file's path, for the failure message.
    workflow : Workflow
        The parsed document.

    Returns
    -------
    list[CoverageLane]
        One entry per coverage-invoking job.
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
    """Return one workflow's jobs, or nothing when it declares none.

    Parameters
    ----------
    workflow : Workflow
        The parsed document.

    Returns
    -------
    list of tuple
        The job identifier and its body, in file order.
    """
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return []
    return [(str(name), job) for name, job in jobs.items()]


def _coverage_steps() -> list[tuple[str, str, int, Step]]:
    """Return every step that invokes the coverage action.

    Flattened into one comprehension rather than three nested loops, so
    the assertions below read the steps rather than walking the
    document to find them.

    Returns
    -------
    list of tuple
        The workflow path, the job identifier, the step's one-based
        position in its job, and the step.
    """
    return [
        (path, name, index + 1, step)
        for path in COVERAGE_WORKFLOWS
        for name, job in _jobs_of(_workflow(path))
        for index, step in enumerate(job.get("steps") or [])
        if COVERAGE_ACTION in str(step.get("uses", ""))
    ]


def _cargo_manifest_of(step: Step) -> object:
    """Return the ``cargo-manifest`` input a step passes, or None.

    Parameters
    ----------
    step : Step
        The coverage step.

    Returns
    -------
    object
        The input's value, or None when the step passes none.
    """
    inputs = typ.cast("dict[str, object]", step).get("with")
    return inputs.get("cargo-manifest") if isinstance(inputs, dict) else None
