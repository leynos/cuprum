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

import typing as typ

import pytest

from cuprum.unittests._timeout_lane_support import (
    CEILING_MARGIN_SECONDS,
    COVERAGE_ACTION,
    COVERAGE_WORKFLOWS,
    EXPECTED_WATCHDOG_SECONDS,
    OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS,
    WATCHDOG_VARIABLE,
    CoverageLane,
    Workflow,
    _cargo_manifest_of,
    _coverage_steps,
    _lanes,
    lanes_in,
    required_ceiling,
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
