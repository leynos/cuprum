"""Keep every root-level test module in the default suite the selector defines.

`PYTEST_TARGETS` decides what `make test` and CI's `typecheck-test` job collect,
and it is a list of glob patterns rather than a directory sweep. A module under
`tests/` that matches no pattern is absent from that suite, so a routine
`make test` on a developer's checkout never executes it: issue #499 found seven
such modules at once, and pull request #488 hit the same problem with four more.

Absent from the default suite is not the same as absent from CI. The `coverage`
job runs a bare `pytest` from the repository root through an out-of-repo
composite action, so it collects the whole tree — a module this guard would
flag as uncovered still executes there, and still gates the merge. What the
selector costs is the fast local loop and the default PR-lane suite, not all
execution; the guard is scoped to the first of those, and the developers' guide
says so.

The failure is invisible from a green run, which is why this module reads the
tree and the selector rather than the test results. One rule pins the
selection: every root-level `tests/test_*.py` module is named by
`PYTEST_TARGETS` or by `ACT_SCENARIO_TARGETS`, or appears in `EXCEPTIONS` as an
`Exemption` naming the selector that collects it, the target that expands that
selector, and a reason (see `tests/helpers/suite_selection.py` for why that
table is empty and how to add to it).

Whether anything *runs* that selector — the recipe consuming it, and the CI
job invoking that recipe on a leg a pull request schedules — is the companion
question, and it lives in `tests/test_ci_suite_wiring_contract.py`. The two
fail differently: a module outside the selector is a missing test reported by
name against the tree, while a broken wiring leaves the tree correctly covered
and CI collecting something else.

The resolution machinery — the exception table, and everything that validates
it — lives in `tests/helpers/suite_selection.py`, so this module holds only the
assertions. The reasoning is recorded under "Test selection" in the
developers' guide.

This module asserts what this tree *does*. Whether the machinery would fail if
it did not is a different claim, and it is driven against seeded faults in
`tests/test_ci_selection_guard_controls.py`; the two were one module until it
crossed the 400-line limit `AGENTS.md` sets and the lint gate enforces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.makefile import variable_expansion
from tests.helpers.suite_selection import (
    SCENARIO_SELECTOR,
    SELECTOR,
    covered_modules,
    exceptions_verified,
    remedy,
    require,
    root_modules,
    selected_paths,
    uncovered,
)


def test_every_root_level_module_has_a_ci_route() -> None:
    """Fail when a module under `tests/` matches no pattern the suite runs.

    This is the defect issue #499 reported: seven contract modules sat under
    `tests/`, matched neither `tests/test_ci_*.py` nor the explicitly named
    `tests/test_native_sdist.py`, and never executed in `make test` or in CI.
    Renaming them into the `test_ci_` selector was the fix; this test is what
    makes the next one fail loudly instead.
    """
    missing = uncovered()
    require(
        condition=not missing,
        message=remedy(missing),
    )


def test_the_six_reported_modules_are_now_collected() -> None:
    """Pin the modules issue #499 named, so a regression is reported by name.

    The population-wide check above would also catch one of these leaving the
    selector, but only as a line in a list. Naming them keeps the issue's own
    finding legible in the test that closes it, so a reader can see the six
    without reconstructing the report.

    Resolved against `PYTEST_TARGETS` alone rather than through
    `covered_modules`, which unions in `ACT_SCENARIO_TARGETS`. These six are
    the *default suite*, and the scenario selector is the suite they were
    deliberately kept out of: a module moved there would leave `make test`
    while an aggregate check still called it covered. Asking the narrower
    question is what makes this test about the issue it closes.

    Issue #499 reported seven modules. The seventh,
    `tests/test_codescene_environment_contract.py`, was retired by #559 along
    with the local CodeScene helpers it read, so its renamed form no longer
    exists and there is no module left to collect.
    """
    expected = {
        "tests/test_ci_coverage_scratch_discard.py",
        "tests/test_ci_dev_fast_action.py",
        "tests/test_ci_loom_workflow_contract.py",
        "tests/test_ci_mutation_workflow_contract.py",
        "tests/test_ci_resource_sampler_action.py",
        "tests/test_ci_setup_sccache_action.py",
    }
    selected = {str(path) for path in selected_paths(variable_expansion(SELECTOR))}
    missing = sorted(expected - selected)
    assert not missing, (
        f"issue #499's six surviving modules must stay in the suite; these "
        f"are no longer collected by {SELECTOR}: {missing}"
    )


def test_covered_modules_follows_the_selector_it_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Show the resolver *tracks* the selector rather than returning the tree.

    The equality check above is necessary but not sufficient on this tree, and
    the reason is the point of issue #499: the whole population is now
    collected, so `root_modules()` and `covered_modules()` agree — and a
    resolver that simply *returned* `root_modules()` would satisfy the equality
    while reading no Makefile at all. The two answers coincide today, so
    comparing them cannot separate "reads the selector" from "returns the
    population".

    This drives them apart. Narrowing the selector to one real module must
    narrow `covered_modules` to that module: a resolver reading the Makefile
    follows, while a constant stays at full population and is caught. The
    module named is real so the narrowed selector cannot fail for being
    unknown, and the control asserts the narrowed set is genuinely smaller
    than the full one, so it cannot pass by the patch not taking effect.

    `variable_expansion` is patched at `tests.helpers.suite_selection`, its
    own module namespace, because that is the name the resolver calls.
    """
    module = root_modules()[0]
    real = variable_expansion
    full = covered_modules()
    monkeypatch.setattr(
        "tests.helpers.suite_selection.variable_expansion",
        lambda name, **kwargs: (module,) if name == SELECTOR else real(name, **kwargs),
    )
    narrowed = covered_modules()
    assert narrowed == frozenset({module}), (
        "a resolver reading the selector must follow it; one returning the "
        f"enumerated population would stay at {len(full)} entries but gave "
        f"{sorted(narrowed)!r}"
    )
    assert len(narrowed) < len(full), (
        "the control only proves anything if the narrowed selector is genuinely "
        f"smaller than the full one; got {len(narrowed)} vs {len(full)}"
    )


def test_the_selector_resolves_the_whole_root_module_population() -> None:
    """Show the selector and the enumeration agree on more than the six.

    The population check and the named check can both pass while the resolver
    reads a stub of the selector, so this asserts the two sets meet: every
    enumerated module except a documented exception is in the expansion, and
    the expansion is larger than the exception-free minimum. A resolver that
    returned only the literal `tests/test_native_sdist.py` entry would satisfy
    neither.
    """
    modules = root_modules()
    covered = covered_modules()
    exempt = exceptions_verified()
    unresolved = sorted(
        module for module in modules if module not in covered and module not in exempt
    )
    assert not unresolved, remedy(unresolved)
    assert len(covered) > 1, (
        "a selector that resolves to a single module cannot be reading the "
        f"patterns; got {sorted(covered)}"
    )


def test_covered_modules_equals_the_direct_selector_expansion() -> None:
    """Pin `covered_modules` to the selectors, not to a plausible constant.

    Every other assertion here asks `covered_modules` a question and checks the
    answer against `root_modules`, so a resolver that returned *every*
    enumerated module would satisfy all of them while reading no selector at
    all: `uncovered()` would be empty by construction and the whole guard would
    be vacuous. That is not hypothetical — replacing the resolver with
    `frozenset(root_modules())` leaves the population check and every
    parametrized case passing.

    So this compares the resolver against the expansion computed here, from the
    two selectors directly, with neither `root_modules` nor the exception table
    involved. The two must name the same files. The comparison is what makes
    `covered_modules` a claim about the Makefile rather than about the tree.

    `test_ci_act_harness_contract.py` pins the *shape* of both selectors — that
    they stay disjoint, and that the scenario target is not folded into the
    default suite. This pins that the resolver reads them, which is the half
    that a constant cannot satisfy.
    """
    # Expand both selectors here rather than through `covered_modules`, so a
    # broken resolver cannot define its own expected answer. The root-level
    # filter is applied here too, and deliberately: `covered_modules` is
    # defined as the root-level subset, so comparing against the unfiltered
    # expansion would fail for the right reason but the wrong test.
    direct = {
        str(path)
        for selector in (SELECTOR, SCENARIO_SELECTOR)
        for pattern in variable_expansion(selector)
        for path in selected_paths((pattern,))
        if path.parent == Path("tests")
    }
    resolved = covered_modules()
    assert resolved == direct, (
        "covered_modules must be exactly what the two selectors expand to; a "
        "resolver disagreeing with the Makefile is reading something else — a "
        f"constant, or one selector rather than both. Only in resolved: "
        f"{sorted(resolved - direct)}; only in the expansion: "
        f"{sorted(direct - resolved)}"
    )


@pytest.fixture(scope="module")
def covered() -> frozenset[str]:
    """Resolve the selector once, for every case the parametrization below spawns.

    `covered_modules` re-reads the Makefile and re-expands both selectors on
    each call, and the test below parametrizes one case per root-level module —
    fifty-one of them at present — so resolving in the test body would parse the
    same file fifty-one times to reach an identical answer. The scope is the
    module because the Makefile cannot change while the suite runs; a fixture
    that outlived a run would start asserting against a stale file.

    Returns
    -------
    frozenset of str
        The root-level modules the two selectors resolve to.

    Raises
    ------
    AssertionError
        If either selector resolves to nothing, or if neither names a
        root-level module; propagated from `covered_modules` so a broken
        selector fails as that resolver's diagnostic rather than as fifty-one
        identical per-module reports.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from covered_modules()
    return covered_modules()


@pytest.mark.parametrize("module", root_modules())
def test_each_root_module_matches_a_selector_pattern(
    module: str, covered: frozenset[str]
) -> None:
    """Report each uncovered module separately, so one does not hide another.

    The population check fails on the first list it builds; this one fails per
    module, which is what a contributor sees in an IDE's test tree: the module
    they added is the red one.

    `exceptions_verified` is called per case rather than folded into the
    fixture: it validates the exception table, so a bad entry should fail as
    the assertion it is on every case that depends on it, not only on the first.
    """
    require(
        condition=module in covered or module in exceptions_verified(),
        message=remedy((module,)),
    )


def test_the_selector_collects_the_modules_that_make_this_claim() -> None:
    """Keep the guard itself inside what the guard describes.

    Every assertion in this module is about what `PYTEST_TARGETS` collects, and
    the module is collected by that same selector — so a selector edit that
    dropped `tests/test_ci_*.py` would silence these assertions rather than fail
    them. Makefile target `test-selection` runs this module by name for exactly
    that reason, and this is the check that names collide: the bootstrap route
    must not be the only thing that runs it.
    """
    selector = SELECTOR
    names = variable_expansion(selector)
    assert "tests/test_ci_*.py" in names, (
        f"{selector} must keep the glob that collects the CI contract modules; "
        f"without it these assertions run only through `make test-selection`, "
        f"and a selector that lost the whole family would go unreported. "
        f"Resolved patterns: {names!r}"
    )


def test_dropping_the_contract_glob_makes_the_bootstrap_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Show the bootstrap fails in the state it exists to catch.

    Makefile target `test-selection` runs this module by name so the guard
    cannot be silenced by the selector it polices. That is only worth anything
    if the guard actually *fails* in that state, so the selector is rewritten
    here as it would be if someone deleted its `tests/test_ci_*.py` pattern.
    Every contract module of this family then falls outside the selector, and
    the guard has to report exactly those.

    The rewrite strips the pattern from the selector's *real* expansion rather
    than from a copy of the Makefile, so the control is about deleting one
    pattern from this repository's selector and not about a fictional one. It
    is injected into the reader rather than written to disk, so this
    repository's Makefile is untouched.
    """
    glob = "tests/test_ci_*.py"
    real = variable_expansion(SELECTOR)
    assert glob in real, (
        f"{SELECTOR} must carry {glob} for this control to remove anything; "
        f"it resolves to {real!r}"
    )
    # Patch the reader in the namespace that consumes it. `suite_selection`
    # binds `variable_expansion` by value at import, so patching the module it
    # was defined in would leave the name it actually calls untouched — and the
    # control would then pass over an unmodified selector, proving nothing.
    monkeypatch.setattr(
        "tests.helpers.suite_selection.variable_expansion",
        lambda name, **_kwargs: (
            tuple(pattern for pattern in real if pattern != glob)
            if name == SELECTOR
            else variable_expansion(name)
        ),
    )
    dropped = tuple(
        module for module in root_modules() if module.startswith("tests/test_ci_")
    )
    assert dropped, (
        "the control needs the glob to be collecting something; with no "
        "tests/test_ci_*.py module on disk it proves nothing"
    )
    reported = uncovered()
    missing = [module for module in dropped if module not in reported]
    assert not missing, (
        f"the bootstrap must report every module the dropped glob stopped "
        f"collecting; unreported: {missing!r}. This is the failure that keeps "
        "`make test-selection` from being a no-op"
    )
