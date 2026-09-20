"""Contract for the timers that can end a coverage run.

Four independent budgets can end a coverage lane, each set somewhere
different, and they only work if each sits above the one inside it:
nextest's per-test allowance and whole-run budget, the shared coverage
action's wall-clock watchdog on the ``cargo`` invocation, and the job's
own ``timeout-minutes``.

The two nextest tiers live in the Cargo workspace's ``.config/nextest.toml``
rather than the repository root's, because nextest resolves that path from
the workspace root and searches no parent directory. Cuprum keeps no root
``Cargo.toml``, so the workspace is ``rust/``.

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

import typing as typ

import pytest

from cuprum.unittests._coverage_timeout_lane_support import (
    CEILING_MARGIN_SECONDS,
    COVERAGE_ACTION,
    COVERAGE_WORKFLOWS,
    OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS,
    CoverageLane,
    Workflow,
    _cargo_manifest_of,
    _coverage_steps,
    _lanes,
    lanes_in,
    required_ceiling,
)
from cuprum.unittests._timeout_lane_support import (
    COLD_BUILD_ALLOWANCE_SECONDS,
    EXPECTED_GLOBAL_TIMEOUT_SECONDS,
    EXPECTED_PER_TEST_ALLOWANCE_SECONDS,
    EXPECTED_WATCHDOG_SECONDS,
    NEXTEST_CONFIG,
    WATCHDOG_VARIABLE,
    _allowance_of,
    _default_nextest_profile,
    _slow_timeout_of,
    global_timeout_seconds,
    largest_per_test_allowance_seconds,
    nextest_config_path,
    termination_allowance_seconds,
)
from tests.helpers.docs import repo_root

REQUIRED_CONDITIONS: typ.Final[dict[tuple[str, str], tuple[object, object]]] = {
    (".github/workflows/ci.yml", "coverage"): (
        None,
        "github.event_name == 'pull_request'",
    ),
    (".github/workflows/coverage-main.yml", "coverage-upload"): (None, None),
}


REQUIRED_CARGO_MANIFEST: typ.Final[str] = "rust/Cargo.toml"


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


def test_the_nextest_config_sits_where_nextest_looks_for_it() -> None:
    """Nextest reads its config from the workspace root, not the repository root.

    Nextest resolves repository configuration from
    `<workspace>/.config/nextest.toml` and searches no parent directory, so a
    copy at the repository root is never read. Cuprum keeps no root
    `Cargo.toml`, making `rust/` the workspace; a config written one level up
    would leave both inner tiers inert while every value assertion below still
    passed, because those read the file rather than the run.

    Proved by mutation: moving the file to the repository root and running
    `cargo nextest list --manifest-path rust/Cargo.toml` exits 0 and reports
    no parse error, while an invalid value at the workspace root exits 96.
    """
    misplaced = repo_root() / ".config" / "nextest.toml"
    assert not misplaced.is_file(), (
        "a nextest configuration at the repository root is never read; nextest "
        f"resolves {NEXTEST_CONFIG} from the Cargo workspace root and searches "
        "no parent directory, so the tiers it declares would be inert beneath "
        "a watchdog sized for neither"
    )
    assert nextest_config_path().is_file(), (
        f"expected the nextest configuration at {NEXTEST_CONFIG}, the path "
        "nextest resolves from the Cargo workspace root"
    )


def test_the_nextest_tiers_are_explicitly_set() -> None:
    """The default profile declares both inner timeout tiers in full.

    Every other assertion in this module compares the tiers with each
    other, and a comparison is satisfied by any pair that orders
    correctly. Narrowing the profile to ``period = "1s"`` with
    ``terminate-after = 1`` keeps ``global-timeout`` the larger of the two
    while killing healthy tests, which is the outcome the per-test tier
    exists to prevent, and a ``global-timeout`` of two seconds bounds the
    whole run below a single test's allowance. The issue's contract states
    the per-test allowance as a bound rather than a measurement -- large
    enough that no healthy test reaches it -- so the values are pinned
    here, and the ordering assertions below are what the pinned values are
    then held to.
    """
    profile = _default_nextest_profile()
    slow_timeout = _slow_timeout_of(profile)
    for key in ("period", "terminate-after"):
        assert key in slow_timeout, (
            f"{NEXTEST_CONFIG} must set [profile.default].slow-timeout.{key}"
        )
    assert "global-timeout" in profile, (
        f"{NEXTEST_CONFIG} must set [profile.default].global-timeout"
    )
    allowance = _allowance_of(slow_timeout)
    assert allowance == EXPECTED_PER_TEST_ALLOWANCE_SECONDS, (
        f"{NEXTEST_CONFIG}'s per-test allowance is {allowance} s, not the "
        f"{EXPECTED_PER_TEST_ALLOWANCE_SECONDS} s this repository sizes for; "
        f"the ordering assertions are satisfied by any pair that merely orders "
        f"correctly, so a profile narrowed towards the largest healthy test "
        f"would still pass them while terminating tests that are working"
    )
    whole_run = global_timeout_seconds()
    assert whole_run == EXPECTED_GLOBAL_TIMEOUT_SECONDS, (
        f"{NEXTEST_CONFIG}'s global-timeout is {whole_run} s, not the "
        f"{EXPECTED_GLOBAL_TIMEOUT_SECONDS} s this repository sizes for; a "
        f"value below a single test's allowance would pass the containment "
        f"assertion while bounding the run shorter than the tests it runs"
    )


def test_the_nextest_global_timeout_contains_the_per_test_allowance() -> None:
    """A whole-run budget must outlast a test's termination budget."""
    assert global_timeout_seconds() > largest_per_test_allowance_seconds(), (
        f"{NEXTEST_CONFIG}'s global-timeout must exceed its largest per-test "
        "allowance (slow-timeout.period multiplied by terminate-after)"
    )


def test_the_nextest_global_timeout_stays_inside_the_cargo_watchdog() -> None:
    """The watchdog must remain an outer tier rather than pre-empting nextest."""
    assert global_timeout_seconds() < EXPECTED_WATCHDOG_SECONDS, (
        f"{NEXTEST_CONFIG}'s global-timeout must stay below the "
        f"{EXPECTED_WATCHDOG_SECONDS} s {WATCHDOG_VARIABLE} watchdog"
    )


def test_the_watchdog_contains_the_global_timeout_and_its_termination() -> None:
    """Tier three must cover everything tier two can spend, and the build.

    The two clocks do not start together and the terms are not the same
    work. The watchdog starts with `cargo` and covers the build; nextest's
    global timeout starts only when tests begin, and hitting it starts a
    termination procedure rather than stopping the run. A watchdog merely
    above the global timeout therefore still cuts off the run while nextest
    is terminating it, and the failure it reports names `cargo` rather than
    the test.

    The cold-build term is the one that makes the watchdog a hang detector
    rather than a schedule. Making the global timeout and the build share
    one budget means a branch's first run, which compiles everything the
    cache cannot serve, can spend the timeout before its tests start and be
    reported as a hang. The shared action sizes for exactly this, warning
    that a build which is merely cold must not look like one.
    """
    global_timeout = global_timeout_seconds()
    termination = termination_allowance_seconds()
    required = global_timeout + termination + COLD_BUILD_ALLOWANCE_SECONDS
    assert required <= EXPECTED_WATCHDOG_SECONDS, (
        f"the {EXPECTED_WATCHDOG_SECONDS} s {WATCHDOG_VARIABLE} watchdog must "
        f"cover {NEXTEST_CONFIG}'s {global_timeout} s global-timeout, the "
        f"{termination} s termination allowance, and the "
        f"{COLD_BUILD_ALLOWANCE_SECONDS} s cold-build allowance, {required} s "
        f"in all; a watchdog that covers only the first two reads a cold "
        f"compile as a hang and reports it against cargo rather than the test"
    )


def test_the_nextest_slow_timeout_terminates_hung_tests() -> None:
    """Slow warnings must eventually kill the test that caused them."""
    assert "terminate-after" in _slow_timeout_of(_default_nextest_profile()), (
        f"{NEXTEST_CONFIG} must set "
        "[profile.default].slow-timeout.terminate-after so a hung test is "
        "killed rather than reported slow indefinitely"
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
    offenders = [
        f"{path}:{job} step {position} passes {_cargo_manifest_of(step)!r}"
        for path, job, position, step in _coverage_steps()
        if _cargo_manifest_of(step) != REQUIRED_CARGO_MANIFEST
    ]
    assert not offenders, (
        f"these coverage steps do not pass cargo-manifest="
        f"{REQUIRED_CARGO_MANIFEST!r}: {offenders}; the action would fall "
        f"back to a repository root that holds no Cargo.toml and measure no "
        f"Rust, while every timer above it still read as correctly ordered"
    )
