"""Contract for the timers that can end a coverage run.

Four independent budgets can end a coverage lane, each set somewhere
different, and they only work if each sits above the one inside it. Two
of the four apply here: the shared coverage action's wall-clock watchdog
on the ``cargo`` invocation, and the job's own ``timeout-minutes``. The
two nextest tiers do not, because there is no ``.config/nextest.toml``
for anyone to have set them in.

Both coverage lanes ran on the action's 1,800 s default until this
contract was written, and nothing in this repository mentioned it. A
budget nobody chose is one nobody can defend, and the failure it
produces names ``cargo`` rather than the test that hung.

``yaml.safe_load`` returns ``typing.Any``, which erases every mistake an
assertion can make about the shape it reads, so the shapes below declare
the keys these tests reach for. Their values stay ``object``, because
they come from files this suite does not control.

See "Test timeouts: the tiers this repository sets" in
``docs/developers-guide.md``, and the canonical wording in
`leynos/shared-actions`' `generate-coverage` README.
"""

from __future__ import annotations

import functools
import typing as typ

import pytest
import yaml

from tests.helpers.docs import repo_root

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


class Step(typ.TypedDict, total=False):
    """One step of a job, declaring only the keys these tests read."""

    name: object
    uses: object
    env: dict[str, object]


class Job(typ.TypedDict, total=False):
    """One job of a workflow, declaring only the keys these tests read."""

    steps: list[Step]
    env: dict[str, object]


class Workflow(typ.TypedDict, total=False):
    """A parsed workflow file, declaring only the keys these tests read."""

    jobs: dict[str, Job]
    env: dict[str, object]


class CoverageLane(typ.NamedTuple):
    """One job that invokes the coverage action, with its budgets.

    Attributes
    ----------
    workflow : str
        The workflow file's path.
    job : str
        The job's identifier.
    steps : int
        How many coverage steps the job runs. Each gets its own watchdog,
        so the job must contain all of their budgets.
    watchdog : int or None
        The budget in force, or None when no level sets one and the lane
        inherits the action's default.
    ceiling : int or None
        The job's ``timeout-minutes``, or None when it declares none.
    """

    workflow: str
    job: str
    steps: int
    watchdog: int | None
    ceiling: int | None

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
                    steps=len(steps),
                    watchdog=_watchdog_of(workflow, job, steps[0]),
                    ceiling=None if raw_ceiling is None else int(str(raw_ceiling)),
                )
            )
    return tuple(found)


def test_both_workflows_carry_a_coverage_lane() -> None:
    """The contract needs lanes to assert against.

    A repin or a rename that stopped the coordinate matching would
    otherwise turn every assertion below into a vacuous pass over an
    empty list, and the loss would look exactly like success. Both
    workflows are named, so losing one is not silently half a pass.
    """
    lanes = _lanes()
    covered = {lane.workflow for lane in lanes}
    assert covered == set(COVERAGE_WORKFLOWS), (
        f"expected a coverage lane in each of {COVERAGE_WORKFLOWS}, found "
        f"{sorted(covered)}"
    )


@pytest.mark.parametrize("lane", _lanes(), ids=str)
def test_every_lane_sets_the_watchdog_rather_than_inheriting_it(
    lane: CoverageLane,
) -> None:
    """A budget nobody chose is one nobody can defend.

    The action kills `cargo` after 1,800 s unless told otherwise, and
    nothing in this repository mentioned that until the value was written
    down. The failure it produces names `cargo` rather than the test that
    hung, so the run reads as an infrastructure fault.
    """
    assert lane.watchdog is not None, (
        f"{lane} does not set {WATCHDOG_VARIABLE} at step, job or workflow "
        f"level, so it inherits the shared action's undocumented 1,800 s "
        f"default"
    )
    assert lane.watchdog == EXPECTED_WATCHDOG_SECONDS, (
        f"{lane} sets {WATCHDOG_VARIABLE}={lane.watchdog}, not the "
        f"{EXPECTED_WATCHDOG_SECONDS} sized in the developers' guide; both "
        f"lanes move together or the pull-request lane stops predicting the "
        f"trunk lane it exists to protect"
    )


@pytest.mark.parametrize("lane", _lanes(), ids=str)
def test_the_ceiling_contains_every_watchdog_and_the_work_around_them(
    lane: CoverageLane,
) -> None:
    """Tier four must not pre-empt tier three.

    The two clocks do not start together. The job timer starts when the
    job starts, before the checkout and the toolchain setup, and it is
    still running through whatever follows the coverage step. The
    watchdog starts when `cargo` does. A ceiling merely above the
    watchdog still cancels the job before the watchdog can report an
    overrun, and a cancellation discards the log that would have
    explained it.

    The requirement multiplies the watchdog by the number of coverage
    steps in the job. Each invocation gets its own, so a job that gained
    a second one could legitimately spend both budgets.
    """
    assert lane.watchdog is not None, str(lane)
    required_seconds = lane.watchdog * lane.steps + OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS
    assert lane.ceiling is not None, (
        f"{lane} runs {lane.steps} watchdog-bounded cargo invocation(s) in a "
        f"job with no timeout-minutes; the outermost tier is missing and "
        f"GitHub's six-hour default applies"
    )
    assert lane.ceiling * 60 >= required_seconds, (
        f"{lane} has a ceiling of {lane.ceiling} minutes, below the "
        f"{required_seconds / 60:.0f} needed to contain {lane.steps} "
        f"watchdog(s) of {lane.watchdog}s plus "
        f"{OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS}s of measured work outside "
        f"them; an overrun would be cancelled rather than reported"
    )


def test_the_nextest_tiers_are_absent_rather_than_unset() -> None:
    """The inner two tiers do not exist here, and their absence is a gap.

    The canonical section has four tiers because nextest contributes two
    of them: a per-test allowance and a whole-run budget. There is no
    `.config/nextest.toml` in this repository, so neither is set.

    That is recorded rather than asserted away. Adding the file would
    give a hung test a bound that names the test rather than `cargo`, and
    this test exists to fail when it appears, so the budgets arrive with
    the guide updated in the same change rather than unbounded beneath a
    watchdog sized for neither.
    """
    config = repo_root() / ".config" / "nextest.toml"
    assert not config.is_file(), (
        "a nextest configuration has appeared; set a per-test slow-timeout "
        "and a global-timeout in it, check the global-timeout sits above the "
        "largest per-test allowance (period multiplied by terminate-after) "
        "and inside the cargo watchdog, and update the developers' guide's "
        "timeout section in the same change"
    )
