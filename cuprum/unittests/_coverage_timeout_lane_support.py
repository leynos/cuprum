"""Read coverage-lane timers from GitHub Actions workflows.

The timeout-ordering contract uses these helpers to resolve a coverage action's
watchdogs, job ceiling, conditions, and manifest inputs without obscuring its
assertions with YAML traversal. Call :func:`_lanes` for the live lanes or
:func:`lanes_in` with a synthetic workflow to exercise the reader.

This module owns the workflow shapes, the readers that walk them, and the
budgets those workflows declare; the nextest tiers live in
``cuprum.unittests._timeout_lane_support``. The two halves read different
files and neither imports the other: only the contract brings them
together, and it is the only place where a watchdog is compared with the
run it bounds.
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

#: What a cold instrumented compile may add before the run reaches a test.
#:
#: An allowance, not a measurement: no coverage run here can be proved
#: cold. These lanes archive no `target`, so sccache carries compiler
#: output and a branch's first run compiles everything it cannot serve.
#: Adopted from the estate's own cold run rather than cuprum's warm one.
#: `generate-coverage`'s README records Netsuke's first trunk run after
#: the same change finishing 2,790 tests at about 512 s and being killed
#: at 600 during report generation. That suite is some twenty-five times
#: the 112 tests measured here, so 600 s is conservative; cuprum's whole
#: warm Rust invocation was 83 s on run 35391248951.
COLD_BUILD_ALLOWANCE_SECONDS: typ.Final[int] = 10 * 60

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
