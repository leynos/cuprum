"""Read nextest's configured timeout tiers for the coverage lanes.

Separated from ``test_timeout_ordering_contract`` so the configuration
reading and the assertions stay legible apart, and so neither module
outgrows the 400-line limit ``AGENTS.md`` sets.

This module reads ``rust/.config/nextest.toml`` and nothing else: the
tiers nextest itself enforces. The workflow-side material -- the
watchdog, the job ceiling and the readers for them -- belongs to
``cuprum.unittests._coverage_timeout_lane_support``, which this module
does not import and which does not import it. Only the contract brings
the two halves together, because it is the only place a nextest tier is
compared against the cargo watchdog outside it.
"""

from __future__ import annotations

import functools
import re
import tomllib
import typing as typ
from pathlib import Path

from tests.helpers.docs import repo_root

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

#: The per-test allowance the default profile must grant. Asserted by value
#: rather than merely as the largest of several, because the ordering
#: assertions only compare tiers with each other: a profile narrowed to
#: ``period = "1s"`` with ``terminate-after = 1`` satisfies every one of them
#: while killing healthy tests, which is the failure this tier exists to
#: prevent. The issue's first clause is an absolute claim -- large enough
#: that no healthy test reaches it -- so only an absolute assertion can hold
#: it.
#:
#: 300 s is five 60 s periods, sized for the tests bounded by their own work.
#: The ``compile_tests`` binaries are bounded by cold-build time instead and
#: carry a separate 600 s override, so the profile's widest healthy occupant is
#: the 62.484 s ``compile_time_ui`` measured in CI with its scratch directory
#: and compiler cache already warm -- the allowance is set well above the
#: cold path because that path belongs to the override, not to this tier.
EXPECTED_PER_TEST_ALLOWANCE_SECONDS: typ.Final[int] = 300

#: The whole-run budget the default profile must declare, as a duration in
#: seconds. Pinned for the same reason as the allowance above: the
#: containment assertion is satisfied by any pair that merely orders
#: correctly, so ``global-timeout = "2s"`` would pass it while bounding the
#: run below a single test's allowance.
#:
#: 1200 s is twenty minutes, and sits above the compile-test tier's 600 s so
#: a single slow ``trybuild`` binary cannot exhaust the whole suite.
EXPECTED_GLOBAL_TIMEOUT_SECONDS: typ.Final[int] = 20 * 60

#: The oldest nextest that can read every key this configuration sets.
#:
#: ``global-timeout`` was added in 0.9.100. Nextest warns about configuration
#: keys it does not recognize and carries on, so an older release drops the
#: whole-run budget silently: the run keeps working, the tier is simply
#: absent, and every assertion here still passes because they read the file
#: rather than the run. That is the same class of failure as a config in the
#: wrong directory, which is why the floor is asserted rather than assumed.
#:
#: The constant is duplicated in the ``Makefile``'s ``NEXTEST_MIN_VERSION``
#: and compared against the file here, so the two cannot drift.
EXPECTED_NEXTEST_MIN_VERSION: typ.Final[str] = "0.9.100"


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
NextestOverride = typ.TypedDict(
    "NextestOverride",
    {
        "filter": object,
        "slow-timeout": SlowTimeout,
    },
    total=False,
)
"""A ``[[profile.default.overrides]]`` table read by the contract.

``filter`` is declared because it is what carries an allowance to particular
binaries: a caller reads it to check the widened tier reaches the tests it was
written for, and a ``NextestProfile`` without it does not admit the lookup.
"""
NextestProfile = typ.TypedDict(
    "NextestProfile",
    {
        "slow-timeout": SlowTimeout,
        "global-timeout": object,
        "overrides": list[NextestOverride],
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


NextestConfig = typ.TypedDict(
    "NextestConfig",
    {
        # Hyphenated, so the functional form is the only one that can express
        # it: a class body parses this key as an annotation's illegal target.
        # Read through `.get` below rather than an attribute.
        "nextest-version": object,
        "profile": NextestProfiles,
    },
    total=False,
)
"""The parsed nextest configuration fields read by the contract.

``nextest-version`` is the floor the configuration declares, consulted before
any tier is read; ``profile`` carries the named profiles, including the
required ``default`` profile.
"""


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


def _slow_timeout_of(declaring: NextestProfile | NextestOverride) -> SlowTimeout:
    """Return an explicit slow-timeout configuration.

    One reader serves the profile and its overrides, because an override's
    table replaces the profile's for the tests its filter matches rather
    than merging into it; reading them separately would let one be reported
    as supplying what the other replaced.

    Parameters
    ----------
    declaring : NextestProfile | NextestOverride
        The profile, or one of its overrides, whose table is read.

    Returns
    -------
    SlowTimeout
        The ``slow-timeout`` mapping that table declares; a table declaring
        none fails the caller's contract assertion rather than being read as
        a tier of zero.
    """
    slow_timeout = declaring.get("slow-timeout")
    assert isinstance(slow_timeout, dict), (
        f"{NEXTEST_CONFIG} must declare slow-timeout as a table, in "
        f"[profile.default] or in an override that widens a tier"
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


def _slow_timeout_overrides(profile: NextestProfile) -> list[NextestOverride]:
    """Return the profile's overrides that declare a ``slow-timeout``.

    Parameters
    ----------
    profile : NextestProfile
        The default profile, whose overrides are read.

    Returns
    -------
    list[NextestOverride]
        One entry per override that sets ``slow-timeout``, whole rather than
        reduced to the timeout, so a caller can also read the ``filter``
        granting that allowance to particular tests. An allowance and a
        filter read from separate lists could report a widened tier
        alongside a filter that does not carry it.
    """
    overrides = profile.get("overrides")
    if overrides is None:
        return []
    assert isinstance(overrides, list), (
        f"{NEXTEST_CONFIG} must declare profile overrides as an array of tables"
    )
    declared: list[NextestOverride] = []
    for override in overrides:
        assert isinstance(override, dict), (
            f"{NEXTEST_CONFIG} must declare each profile override as a table"
        )
        if "slow-timeout" in override:
            declared.append(override)
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
    overrides = _slow_timeout_overrides(profile)
    declared = [_slow_timeout_of(profile)]
    declared.extend(_slow_timeout_of(override) for override in overrides)
    return max(_allowance_of(slow_timeout) for slow_timeout in declared)


def declared_minimum_nextest_version() -> str:
    """Return the nextest version this configuration declares it needs.

    Returns
    -------
    str
        The ``nextest-version`` the configuration file states, as written.

    Nextest refuses to start below a declared requirement, so this is the
    one setting that protects the tiers below it. Without it a release that
    predates ``global-timeout`` parses the file, warns about the key it does
    not know, and runs the suite with no whole-run budget while every
    assertion here still passes. A missing declaration fails the caller's
    contract assertion rather than being read as an unconstrained floor.
    """
    declared = _nextest_config().get("nextest-version")
    assert declared is not None, (
        f"{NEXTEST_CONFIG} must declare nextest-version; without it a release "
        f"that predates an option in this file drops that option silently and "
        f"the run it was meant to bound proceeds unbounded"
    )
    return str(declared)


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
        The largest configured ``slow-timeout.grace-period``, across the
        default profile and every override that sets a ``slow-timeout``,
        with a 60-second floor applied to it.

    An allowance rather than a reading of nextest. Nextest's own default is
    10 seconds, so the floor here is this repository's, not the tool's, and
    a grace period configured below it is modelled as 60. That can only
    raise the sum the watchdog must contain, never lower it, which is the
    safe direction for a budget whose purpose is to avoid reporting a slow
    build as a hang. Cuprum configures no grace period, so the floor is
    what this returns today.

    Every override is read, and read whole. An override's ``slow-timeout``
    replaces the profile's table for the tests its filter matches rather
    than merging into it — proved by running a binary under an override
    whose period and multiplier gave 2 s while the profile's gave 300 s,
    and seeing nextest terminate at 2.014 s — so an override that declares
    a grace period has replaced whatever the profile declared, and the
    profile's value cannot speak for those tests. Reading only the profile
    would let a widened grace period go uncontained by the watchdog, and
    the failure that produces names ``cargo`` rather than the test.
    """
    profile = _default_nextest_profile()
    declared = [_slow_timeout_of(profile)]
    declared.extend(
        _slow_timeout_of(override) for override in _slow_timeout_overrides(profile)
    )
    configured = [
        _duration_seconds(slow_timeout["grace-period"])
        for slow_timeout in declared
        if "grace-period" in slow_timeout
    ]
    return max(60, max(configured, default=0))
