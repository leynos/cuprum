"""Read nextest's configured timeout tiers for the coverage lanes.

Separated from ``test_timeout_ordering_contract`` so the configuration
reading and the assertions stay legible apart, and so neither module
outgrows the 400-line limit ``AGENTS.md`` sets. The workflow-side readers
live in ``cuprum.unittests._coverage_timeout_lane_support``, which this
module does not import: nextest's tiers and GitHub's are read from
different files and only the contract brings the two together.
"""

from __future__ import annotations

import functools
import re
import tomllib
import typing as typ
from pathlib import Path

from tests.helpers.docs import repo_root

#: The workflows carrying a coverage lane. Both are read, so a lane that
#: gained a budget in one and lost it in the other cannot pass by being
#: half right.
COVERAGE_WORKFLOWS: typ.Final[tuple[str, ...]] = (
    ".github/workflows/ci.yml",
    ".github/workflows/coverage-main.yml",
)

#: The Cargo workspace that nextest reads its repository configuration
#: for, as a path below the repository root. Cuprum keeps no root
#: ``Cargo.toml``, so the workspace is the ``rust/`` crate tree rather
#: than the repository root.
CARGO_WORKSPACE_DIR: typ.Final[Path] = Path("rust")

#: The repository configuration carrying nextest's inner timeout tiers.
#:
#: Below the Cargo workspace root, not the repository root, because nextest
#: resolves ``.config/nextest.toml`` from the former and searches no parent
#: directories. A copy at the repository root is never read, so the tiers it
#: declares would be inert while every assertion passed. Built by division
#: so the workspace segment is spelled once, and relative to the repository
#: root like every other path the contract reads.
NEXTEST_CONFIG: typ.Final[Path] = CARGO_WORKSPACE_DIR / ".config" / "nextest.toml"

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
        "overrides": list[dict[str, object]],
    },
    total=False,
)
"""The nextest profile fields that define the inner timeout tiers.

``overrides`` carries any ``[[profile.default.overrides]]`` tables, because a
per-test override can raise one test's allowance above the profile's own.
"""


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


def nextest_config_path() -> Path:
    """Return the nextest configuration, below the repository root.

    Returns
    -------
    Path
        The configuration nextest itself resolves.
    """
    return repo_root() / NEXTEST_CONFIG


@functools.cache
def _nextest_config() -> NextestConfig:
    """Parse the repository's nextest configuration."""
    parsed = tomllib.loads(nextest_config_path().read_text(encoding="utf-8"))
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


def _allowance_of(slow_timeout: SlowTimeout) -> int:
    """Return one slow-timeout's termination budget in seconds.

    Parameters
    ----------
    slow_timeout : SlowTimeout
        A profile's or an override's ``slow-timeout`` mapping.

    Returns
    -------
    int
        ``period`` multiplied by ``terminate-after``.

    A missing ``terminate-after`` is refused rather than read as zero. It
    means nextest warns and never kills the test, so that test's budget is
    unbounded and no outer tier can be said to contain it. Reading it as
    zero would let the caller's maximum quietly skip the one declaration
    that cannot be contained.
    """
    period = slow_timeout.get("period")
    assert period is not None, f"{NEXTEST_CONFIG} must set slow-timeout.period"
    terminate_after = slow_timeout.get("terminate-after")
    assert terminate_after is not None, (
        f"{NEXTEST_CONFIG} declares a slow-timeout period without "
        "terminate-after, which reports a test as slow without ever killing "
        "it; a budget that never terminates cannot be contained by the tiers "
        "outside it"
    )
    return _duration_seconds(period) * int(str(terminate_after))


def _override_slow_timeouts(profile: NextestProfile) -> list[SlowTimeout]:
    """Return every ``slow-timeout`` the profile's overrides declare.

    Parameters
    ----------
    profile : NextestProfile
        The default profile, whose overrides are read.

    Returns
    -------
    list[SlowTimeout]
        One entry per override that sets ``slow-timeout``.
    """
    overrides = profile.get("overrides")
    if overrides is None:
        return []
    assert isinstance(overrides, list), (
        f"{NEXTEST_CONFIG} must declare profile overrides as an array of tables"
    )
    declared: list[SlowTimeout] = []
    for override in overrides:
        assert isinstance(override, dict), (
            f"{NEXTEST_CONFIG} must declare each profile override as a table"
        )
        if "slow-timeout" in override:
            declared.append(_slow_timeout_of(typ.cast("NextestProfile", override)))
    return declared


def largest_per_test_allowance_seconds() -> int:
    """Return the largest per-test termination budget a test can reach.

    Returns
    -------
    int
        The largest ``slow-timeout.period`` multiplied by
        ``terminate-after``, across the default profile and every override
        it declares.

    An override may grant a test a longer budget than the profile it
    overrides, so ``profile.default.slow-timeout`` alone does not bound
    those tests. Reading only the profile would report the allowance as
    smaller than it is, and the containment assertions would then pass
    while a test could still outlast the tier meant to contain it. Missing
    required fields fail the contract assertion that reads them.
    """
    profile = _default_nextest_profile()
    declared = [_slow_timeout_of(profile), *_override_slow_timeouts(profile)]
    return max(_allowance_of(slow_timeout) for slow_timeout in declared)


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
