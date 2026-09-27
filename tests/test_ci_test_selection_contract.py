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
"""

from __future__ import annotations

import pytest

from tests.helpers.makefile import variable_expansion
from tests.helpers.suite_selection import (
    EXCEPTIONS,
    SCENARIO_SELECTOR,
    SELECTOR,
    Exemption,
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


def test_the_exception_mechanism_reports_an_uncovered_module() -> None:
    """Drive the guard with a module that has no route, and see it objected to.

    The exception table is empty, so the loop in
    `test_every_root_level_module_has_a_ci_route` currently filters nothing and
    a broken comparison could pass unnoticed. This is the seeded fault: a
    module name that cannot be covered by any selector, which the same
    machinery must report through `remedy`. If the guard ever stops detecting
    an uncollected module, this fails first and by name.
    """
    absent = "tests/test_a_module_that_does_not_exist.py"
    assert absent not in root_modules(), (
        "the seeded fault must not exist on disk, or the control proves nothing"
    )
    assert absent not in covered_modules(), (
        "the seeded fault must not be covered, or the control proves nothing"
    )
    message = remedy((absent,))
    assert absent in message, (
        f"the failure message must name the uncovered module; got {message!r}"
    )
    assert "test_ci_" in message, (
        "the failure message must name the rename fix, so a reader is told "
        f"what to do rather than only what is wrong; got {message!r}"
    )
    assert SELECTOR in message, (
        f"the failure message must name the {SELECTOR} fix; got {message!r}"
    )


def test_a_bad_exception_entry_is_rejected_rather_than_silencing_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Show a malformed exemption raises instead of hiding its module.

    `EXCEPTIONS` is empty on this tree, so nothing exercises the verification
    that makes an entry trustworthy — and an unverified entry would be a way
    to hide an uncollected module. The seeded fault exempts a real module
    while naming a selector that does not resolve to it, which is exactly the
    claim `exceptions_verified` exists to refuse.

    The module named is one this tree really has, so the check cannot pass
    because the name was unknown; the selector named is a real Makefile
    variable that resolves to a different file, so it cannot pass by being
    undefined either.
    """
    module = root_modules()[0]
    assert module in covered_modules(), (
        f"{module} must already be collected, or the seeded entry is not the "
        "fault under test"
    )
    monkeypatch.setitem(
        EXCEPTIONS,
        module,
        Exemption(
            selector="ACT_PARSER_TARGETS",
            target="test-python",
            reason="seeded fault: wrong selector",
        ),
    )
    with pytest.raises(AssertionError, match=r"does not resolve to it"):
        exceptions_verified()


def test_an_exemption_naming_a_target_that_ignores_its_selector_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Show a target that does not expand its selector cannot exempt a module.

    A selector and the target that runs it are two claims, and satisfying one
    proves nothing about the other: a target whose recipe never expands the
    named selector runs some *other* suite, so an exemption pairing them would
    record a route that does not exist. The seeded fault does exactly that —
    it names a selector that genuinely collects the module, and a real target
    that genuinely runs in CI, but a target whose recipe does not expand that
    selector.

    Both halves are real, so the entry cannot fail for being unknown; only the
    pairing is wrong. This is the claim the two-field `Exemption` exists to
    make checkable: with the selector and the target collapsed into one
    string, this fault could not be expressed, let alone detected.
    """
    scenario = "tests/integration/test_workflow_integration.py"
    assert scenario in {
        str(path) for path in selected_paths(variable_expansion(SCENARIO_SELECTOR))
    }, (
        f"{scenario} must be collected by {SCENARIO_SELECTOR}, or the seeded "
        "entry is not the fault under test"
    )
    monkeypatch.setitem(
        EXCEPTIONS,
        scenario,
        Exemption(
            selector=SCENARIO_SELECTOR,
            target="test-python",
            reason="seeded fault: target ignores the selector",
        ),
    )
    with pytest.raises(AssertionError, match=r"does not expand"):
        exceptions_verified()


def test_a_module_the_selector_drops_is_reported_as_uncovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Show a module leaving the selector reaches the failure list by name.

    The population check asserts `uncovered()` is empty, so on a healthy tree
    it never proves the machinery can find anything. This subtracts one real
    module from the resolved set — the state a module would be in if it were
    renamed out of the selector — and asserts the module comes back out,
    named, with both fixes in the message.
    """
    module = root_modules()[0]
    monkeypatch.setattr(
        "tests.helpers.suite_selection.covered_modules",
        lambda: frozenset(covered_modules() - {module}),
    )
    missing = uncovered()
    assert module in missing, (
        f"a module the selector no longer names must be reported; got {missing!r}"
    )
    message = remedy(missing)
    assert module in message, f"the report must name the module; got {message!r}"
    assert "test_ci_" in message, (
        f"the report must name the rename fix; got {message!r}"
    )


def test_the_seven_reported_modules_are_now_collected() -> None:
    """Pin the modules issue #499 named, so a regression is reported by name.

    The population-wide check above would also catch one of these leaving the
    selector, but only as a line in a list. Naming them keeps the issue's own
    finding legible in the test that closes it, so a reader can see the seven
    without reconstructing the report.

    Resolved against `PYTEST_TARGETS` alone rather than through
    `covered_modules`, which unions in `ACT_SCENARIO_TARGETS`. These seven are
    the *default suite*, and the scenario selector is the suite they were
    deliberately kept out of: a module moved there would leave `make test`
    while an aggregate check still called it covered. Asking the narrower
    question is what makes this test about the issue it closes.
    """
    expected = {
        "tests/test_ci_codescene_environment_contract.py",
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
        f"issue #499's seven modules must stay in the suite; these are no "
        f"longer collected by {SELECTOR}: {missing}"
    )


def test_the_selector_resolves_the_whole_root_module_population() -> None:
    """Show the selector and the enumeration agree on more than the seven.

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


@pytest.mark.parametrize("module", root_modules())
def test_each_root_module_matches_a_selector_pattern(module: str) -> None:
    """Report each uncovered module separately, so one does not hide another.

    The population check fails on the first list it builds; this one fails per
    module, which is what a contributor sees in an IDE's test tree: the module
    they added is the red one.
    """
    require(
        condition=module in covered_modules() or module in exceptions_verified(),
        message=remedy((module,)),
    )
