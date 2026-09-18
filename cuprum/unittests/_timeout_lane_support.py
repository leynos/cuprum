"""Read the configured timeout tiers for coverage lanes and nextest.

Separated from ``test_timeout_ordering_contract`` so the workflow
reading and the assertions stay legible apart, and so neither module
outgrows the 400-line limit ``AGENTS.md`` sets.
"""

from __future__ import annotations

import functools
import re
import tomllib
import typing as typ

import yaml

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from pathlib import Path

#: The workflows carrying a coverage lane. Both are read, so a lane that
#: gained a budget in one and lost it in the other cannot pass by being
#: half right.
COVERAGE_WORKFLOWS: typ.Final[tuple[str, ...]] = (
    ".github/workflows/ci.yml",
    ".github/workflows/coverage-main.yml",
)

#: The Cargo workspace that nextest reads its repository configuration
#: for. Cuprum keeps no root ``Cargo.toml``, so the workspace is the
#: ``rust/`` crate tree rather than the repository root.
CARGO_WORKSPACE_DIR: typ.Final[str] = "rust"

#: The repository configuration carrying nextest's inner timeout tiers.
#:
#: Relative to the Cargo workspace root, not the repository root, because
#: nextest resolves ``.config/nextest.toml`` from the former and searches
#: no parent directories. A copy at the repository root is never read, so
#: the tiers it declares would be inert while every assertion passed.
NEXTEST_CONFIG: typ.Final[str] = f"{CARGO_WORKSPACE_DIR}/.config/nextest.toml"

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


SlowTimeout = typ.TypedDict(
    "SlowTimeout",
    {
        "period": object,
        "terminate-after": object,
        "grace-period": object,
    },
    total=False,
)
"""The nextest slow-timeout fields read by the contract.

``period`` and ``terminate-after`` together set a test's termination budget;
``grace-period`` supplies the termination allowance for the outer watchdog.
"""
NextestProfile = typ.TypedDict(
    "NextestProfile",
    {
        "slow-timeout": SlowTimeout,
        "global-timeout": object,
    },
    total=False,
)
"""The nextest profile fields that define the inner timeout tiers."""


class NextestProfiles(typ.TypedDict, total=False):
    """The named nextest profiles read by the contract.

    Attributes
    ----------
    default : NextestProfile
        The inherited profile that the coverage action selects by default.
    """

    default: NextestProfile


class NextestConfig(typ.TypedDict, total=False):
    """The parsed nextest configuration fields read by the contract.

    Attributes
    ----------
    profile : NextestProfiles
        Named profiles, including the required ``default`` profile.
    """

    profile: NextestProfiles


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


def cargo_workspace_dir() -> Path:
    """Return the Cargo workspace root nextest resolves its config from.

    Returns
    -------
    Path
        The workspace directory below the repository root.
    """
    return repo_root() / CARGO_WORKSPACE_DIR


@functools.cache
def _nextest_config() -> NextestConfig:
    """Parse the repository's nextest configuration."""
    parsed = tomllib.loads((repo_root() / NEXTEST_CONFIG).read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{NEXTEST_CONFIG} must parse to a mapping"
    return typ.cast("NextestConfig", parsed)


def _default_nextest_profile() -> NextestProfile:
    """Return the default nextest profile the coverage lane uses."""
    profiles = _nextest_config().get("profile")
    assert isinstance(profiles, dict), f"{NEXTEST_CONFIG} must declare [profile]"
    default = profiles.get("default")
    assert isinstance(default, dict), f"{NEXTEST_CONFIG} must declare [profile.default]"
    return default


def _slow_timeout_of(profile: NextestProfile) -> SlowTimeout:
    """Return a profile's explicit slow-timeout configuration."""
    slow_timeout = profile.get("slow-timeout")
    assert isinstance(slow_timeout, dict), (
        f"{NEXTEST_CONFIG} must set [profile.default].slow-timeout"
    )
    return slow_timeout


def _duration_seconds(value: object) -> int:
    """Parse a nextest duration made from contiguous s, m, and h parts."""
    text = str(value)
    position = 0
    total = 0
    for match in re.finditer(r"(\d+)([smh])", text):
        assert match.start() == position, f"unsupported nextest duration {text!r}"
        amount = int(match.group(1))
        unit = match.group(2)
        total += amount * {"s": 1, "m": 60, "h": 60 * 60}[unit]
        position = match.end()
    assert position == len(text), f"unsupported nextest duration {text!r}"
    assert position > 0, f"unsupported nextest duration {text!r}"
    return total


def largest_per_test_allowance_seconds() -> int:
    """Return the default profile's per-test termination budget.

    Returns
    -------
    int
        ``slow-timeout.period`` multiplied by ``terminate-after`` in seconds.

    Missing required fields fail the contract assertion that reads them.
    """
    slow_timeout = _slow_timeout_of(_default_nextest_profile())
    period = slow_timeout.get("period")
    assert period is not None, (
        f"{NEXTEST_CONFIG} must set [profile.default].slow-timeout.period"
    )
    terminate_after = slow_timeout.get("terminate-after")
    assert terminate_after is not None, (
        f"{NEXTEST_CONFIG} must set [profile.default].slow-timeout.terminate-after"
    )
    return _duration_seconds(period) * int(str(terminate_after))


def global_timeout_seconds() -> int:
    """Return the default profile's ``global-timeout`` in seconds.

    Returns
    -------
    int
        The duration configured by ``profile.default.global-timeout``.

    A missing setting fails the contract assertion that reads it.
    """
    global_timeout = _default_nextest_profile().get("global-timeout")
    assert global_timeout is not None, (
        f"{NEXTEST_CONFIG} must set [profile.default].global-timeout"
    )
    return _duration_seconds(global_timeout)


def termination_allowance_seconds() -> int:
    """Return the termination allowance the outer watchdog must cover.

    Returns
    -------
    int
        The larger of the default profile's ``slow-timeout.grace-period``
        and 60 seconds.

    An allowance rather than a reading of nextest. Nextest's own default is
    10 seconds, so the floor here is this repository's, not the tool's, and
    a grace period configured below it is modelled as 60. That can only
    raise the sum the watchdog must contain, never lower it, which is the
    safe direction for a budget whose purpose is to avoid reporting a slow
    build as a hang. Cuprum configures no grace period, so the floor is
    what this returns today.
    """
    grace_period = _slow_timeout_of(_default_nextest_profile()).get("grace-period")
    return max(60, _duration_seconds(grace_period) if grace_period else 0)


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
