"""Prove the selection guard can fail, which is not what its contract proves.

`tests/test_ci_test_selection_contract.py` asserts the rule this tree obeys:
every root-level module has a route into the suite, `uncovered()` is empty, and
the resolver reads the selectors rather than a constant. On a healthy tree every
one of those assertions passes, and that is the whole trouble — a guard that
returned nothing, refused nothing, or subtracted nothing satisfies them all.

So the machinery is driven here against faults the estate does not have:

* a module with no route at all, reported by name and with both fixes;
* an exemption entry whose selector does not resolve to its module;
* an exemption pairing a real selector with a target that never expands it;
* a module subtracted from the resolved set, as a rename out of the selector
  would leave it.

Every fault is seeded through `monkeypatch`, so this repository's `EXCEPTIONS`
table stays empty and its Makefile untouched, and the module under test is the
real one: the control cannot pass because the name was fabricated.

The split from the contract module is the *kind of claim* being made. That
module asks whether this tree obeys the rule; this one asks whether the rule
would be enforced if it did not. They were one module until it crossed the
400-line limit `AGENTS.md` sets and the lint gate enforces.
"""

from __future__ import annotations

import pytest

from tests.helpers.makefile import variable_expansion
from tests.helpers.suite_selection import (
    EXCEPTIONS,
    SCENARIO_SELECTOR,
    Exemption,
    covered_modules,
    exceptions_verified,
    remedy,
    root_modules,
    selected_paths,
    uncovered,
)


def test_the_exception_mechanism_reports_an_uncovered_module() -> None:
    """Drive the guard with a module that has no route, and see it objected to.

    The exception table is empty, so the loop in the population check currently
    filters nothing and a broken comparison could pass unnoticed. This is the
    seeded fault: a module name that cannot be covered by any selector, which
    the same machinery must report through `remedy`. If the guard ever stops
    detecting an uncollected module, this fails first and by name.
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
    assert "PYTEST_TARGETS" in message, (
        f"the failure message must name the PYTEST_TARGETS fix; got {message!r}"
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
