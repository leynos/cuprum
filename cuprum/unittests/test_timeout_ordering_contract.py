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

if typ.TYPE_CHECKING:
    import collections.abc as cabc

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
REQUIRED_CONDITIONS: typ.Final[dict[tuple[str, str], tuple[object, object]]] = {
    (".github/workflows/ci.yml", "coverage"): (
        None,
        "github.event_name == 'pull_request'",
    ),
    (".github/workflows/coverage-main.yml", "coverage-upload"): (None, None),
}


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
    unstated = [
        index + 1 for index, budget in enumerate(lane.watchdogs) if budget is None
    ]
    assert not unstated, (
        f"{lane} leaves step(s) {unstated} of {len(lane.watchdogs)} without "
        f"{WATCHDOG_VARIABLE} at step, job or workflow level, so they inherit "
        f"the shared action's undocumented 1,800 s default"
    )
    wrong = [budget for budget in lane.watchdogs if budget != EXPECTED_WATCHDOG_SECONDS]
    assert not wrong, (
        f"{lane} sets {WATCHDOG_VARIABLE}={wrong}, not the "
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

    The requirement sums each coverage step's own watchdog rather than
    multiplying one of them by the step count. Each invocation gets its
    own budget and they need not agree, so the multiplication describes
    a job only while its steps happen to match. It also carries a margin
    above that sum, because a ceiling equal to it cancels the job at the
    moment the watchdog would have reported the overrun.
    """
    budgets = [budget for budget in lane.watchdogs if budget is not None]
    assert len(budgets) == len(lane.watchdogs), str(lane)
    required_seconds = required_ceiling(budgets)
    assert lane.ceiling is not None, (
        f"{lane} runs {len(budgets)} watchdog-bounded cargo invocation(s) in "
        f"a job with no timeout-minutes; the outermost tier is missing and "
        f"GitHub's six-hour default applies"
    )
    assert lane.ceiling * 60 >= required_seconds, (
        f"{lane} has a ceiling of {lane.ceiling} minutes, below the "
        f"{required_seconds / 60:.0f} needed to contain {len(budgets)} "
        f"watchdog(s) totalling {sum(budgets)}s, "
        f"{OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS}s of measured work outside "
        f"them, and a {CEILING_MARGIN_SECONDS}s margin above that sum; an "
        f"overrun would be cancelled rather than reported"
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


def test_the_required_ceiling_sums_the_watchdogs_and_adds_the_margin() -> None:
    """Two coverage steps need both budgets, plus the margin.

    Both lanes here run the action once, so the sum and a multiple of
    the first agree, and both ceilings clear the smaller requirement
    too. Neither the sum nor the margin is therefore visible to the
    assertion over the workflows, so both are driven with controlled
    numbers.
    """
    assert required_ceiling([2700, 1800]) == (
        4500 + OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS + CEILING_MARGIN_SECONDS
    ), "two steps need the sum of their budgets, not a multiple of one"
    assert required_ceiling([2700]) == (
        2700 + OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS + CEILING_MARGIN_SECONDS
    ), "one step needs its own budget, the allowance and the margin"
    assert required_ceiling([]) == (
        OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS + CEILING_MARGIN_SECONDS
    ), "the margin is a term of its own, not a fraction of the others"


def test_every_coverage_step_carries_its_own_budget() -> None:
    """A job's steps are read individually, not through the first.

    Reading `steps[0]` and multiplying gives the same answer while the
    budgets agree, which they do here. It stops giving the same answer
    the moment a lane raises one of them, which is the change this
    reading exists to survive.
    """
    document = typ.cast(
        "Workflow",
        {
            "jobs": {
                "coverage": {
                    "timeout-minutes": 120,
                    "steps": [
                        {
                            "uses": f"{COVERAGE_ACTION}@abc",
                            "env": {WATCHDOG_VARIABLE: "2700"},
                        },
                        {
                            "uses": f"{COVERAGE_ACTION}@abc",
                            "env": {WATCHDOG_VARIABLE: "1800"},
                        },
                    ],
                }
            }
        },
    )
    (lane,) = lanes_in("synthetic.yml", document)

    assert lane.watchdogs == (2700, 1800), (
        f"each step's own budget must be read, got {lane.watchdogs}; reading "
        f"the first and repeating it would give (2700, 2700)"
    )


def test_each_coverage_lane_carries_the_condition_it_is_meant_to() -> None:
    """A skipped step runs no `cargo`, so its watchdog never arms.

    Every assertion above reads a lane's declared budgets and says
    nothing about whether the step runs. `if: false` on the step or on
    its job would leave a lane that looks bounded and is not, and this
    contract would certify it. So would a plausible condition that
    quietly excluded the event the lane exists for.

    The conditions are pinned by value rather than tested for falsity,
    because YAML parses `false` to a boolean and enumerating falsy
    spellings would miss the plausible ones anyway. The coordinates are
    compared both ways first, so a new lane with no entry here fails
    rather than passing unexamined.

    Proved by mutation: `if: false` on the coverage step, the same on
    its job, the pull-request condition changed to a push-only one, and
    a coordinate dropped from ``REQUIRED_CONDITIONS`` each fail this
    test.
    """
    found = {(lane.workflow, lane.job): lane.conditions for lane in _lanes()}
    assert set(found) == set(REQUIRED_CONDITIONS), (
        f"the coverage lanes are not the ones this contract pins: "
        f"unlisted {sorted(set(found) - set(REQUIRED_CONDITIONS))}, missing "
        f"{sorted(set(REQUIRED_CONDITIONS) - set(found))}; a lane with no "
        f"entry here is a lane whose condition nobody has judged"
    )
    wrong = {
        coordinate: (expected, found[coordinate])
        for coordinate, expected in REQUIRED_CONDITIONS.items()
        if set(found[coordinate]) != {expected}
    }
    assert not wrong, (
        f"these coverage lanes do not carry the conditions the developers' "
        f"guide records, as expected versus found: {wrong}; a lane that is "
        f"skipped runs no cargo, so its watchdog never arms"
    )


#: The manifest every coverage step must hand the shared action, because
#: this repository has no root ``Cargo.toml``.
#:
#: The action decides whether to run `cargo` at all from the manifest it
#: is given, falling back to the repository root. Here that fallback
#: finds nothing, so a step that lost this input would measure no Rust
#: and the watchdog above it would bound an invocation that never
#: happened. Every budget in this contract would still pass.
REQUIRED_CARGO_MANIFEST: typ.Final[str] = "rust/Cargo.toml"


def test_every_coverage_step_names_the_manifest_this_repository_keeps() -> None:
    """The Rust crate is under `rust/`, not at the repository root.

    The shared action resolves `cargo` from the manifest it is handed
    and falls back to the root, which here holds no `Cargo.toml`. A
    coverage step that dropped `cargo-manifest` would therefore run no
    Rust while every timer above it still read as correctly ordered, so
    the input is pinned by value rather than left to the workflow.

    Proved by mutation: removing the input from either step, and
    pointing it at a manifest that does not exist, each fail this test.
    """
    offenders: list[str] = []
    for path in COVERAGE_WORKFLOWS:
        workflow = _workflow(path)
        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for name, job in jobs.items():
            for index, step in enumerate(job.get("steps") or []):
                if COVERAGE_ACTION not in str(step.get("uses", "")):
                    continue
                inputs = typ.cast("dict[str, object]", step).get("with")
                manifest = (
                    inputs.get("cargo-manifest") if isinstance(inputs, dict) else None
                )
                if manifest != REQUIRED_CARGO_MANIFEST:
                    offenders.append(
                        f"{path}:{name} step {index + 1} passes {manifest!r}"
                    )
    assert not offenders, (
        f"these coverage steps do not pass cargo-manifest="
        f"{REQUIRED_CARGO_MANIFEST!r}: {offenders}; the action would fall "
        f"back to a repository root that holds no Cargo.toml and measure no "
        f"Rust, while every timer above it still read as correctly ordered"
    )
