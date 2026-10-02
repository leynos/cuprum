"""Prove an exemption is only honoured when the route it names really exists.

`tests/test_ci_test_selection_contract.py` owns the population question — every
root-level module has a route — and asserts `uncovered()` is empty. On a healthy
tree that assertion never exercises the *pass* branch of
`tests/helpers/suite_selection.exceptions_verified`: the table is empty, so the
loop body runs zero times and the branch that adds a module to `verified` is
never reached. A reader that returned nothing at all would satisfy every
assertion above it.

These are the cases that branch needs. A valid exemption must be honoured and
must remove its module from `uncovered()`, so the mechanism is shown to work
rather than merely to refuse. Each way of writing an entry that names a route
the estate does not have — a selector that resolves elsewhere, a target that
does not consume the selector, a selector that survives only inside a comment
or an echo, no workflow step running the target — must be refused instead.

Every case seeds the entry through `monkeypatch`, so the estate's own empty
table is untouched and the module under test is real: the control cannot pass
because the name was fabricated.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.makefile import (
    makeutil_document,
    recipe_of,
    variable_expansion,
)
from tests.helpers.suite_selection import (
    EXCEPTIONS,
    SCENARIO_SELECTOR,
    Exemption,
    covered_modules,
    exceptions_verified,
    require,
    root_modules,
    selected_paths,
    uncovered,
)

#: A real module of this tree, collected by `SELECTOR`. Naming a module that
#: exists is what stops a refusal test from passing on an unknown name.
_VALID_MODULE = "tests/test_native_sdist.py"


def _seed(monkeypatch: pytest.MonkeyPatch, module: str, entry: Exemption) -> None:
    """Install one exemption for the duration of a test."""
    monkeypatch.setitem(EXCEPTIONS, module, entry)


def _target_recipe(monkeypatch: pytest.MonkeyPatch, recipe: str) -> None:
    """Serve a chosen recipe for every target the exemption machinery reads.

    Seeding the reader rather than the Makefile keeps the control independent of
    the estate's own file: the fault is the *shape* of a recipe, and writing it
    into the Makefile would make the test a claim about that file instead.
    """
    monkeypatch.setattr(
        "tests.helpers.suite_selection.recipe_of",
        lambda _name: recipe,
    )


def test_a_valid_exemption_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Show the pass branch: a fully supported entry is verified.

    This is the branch the population assertion never reaches. Without it a
    reader could stop verifying anything — returning the empty set, or dropping
    every entry — and every other test in this family would still pass, because
    they all assert refusals.

    The entry names the module's real selector, the real target that consumes
    it, and a workflow step that runs that target, so all three claims hold and
    the module comes back in the verified set.
    """
    assert _VALID_MODULE in root_modules(), (
        f"{_VALID_MODULE} must be a module of this tree, or the entry under "
        "test is not the success case"
    )
    _seed(
        monkeypatch,
        _VALID_MODULE,
        Exemption(
            selector="PYTEST_TARGETS",
            target="test-python",
            reason="seeded control: a valid entry",
        ),
    )
    assert _VALID_MODULE in exceptions_verified(), (
        "an entry whose three claims all hold must be verified; a reader that "
        "returned nothing would satisfy every refusal test in this family"
    )


def test_a_valid_exemption_returns_its_module_to_the_covered_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Show a verified exemption is actually subtracted from `uncovered()`.

    Verification and subtraction are two steps, and only the first is proven by
    the test above. This removes the module from the selector's own expansion
    and requires the exemption to take its place, so `uncovered()` is shown to
    consult the verified set rather than the table's keys.
    """
    _seed(
        monkeypatch,
        _VALID_MODULE,
        Exemption(
            selector="PYTEST_TARGETS",
            target="test-python",
            reason="seeded control: a valid entry",
        ),
    )
    without = frozenset(
        path for path in covered_modules() if str(path) != _VALID_MODULE
    )
    monkeypatch.setattr(
        "tests.helpers.suite_selection.covered_modules",
        lambda: without,
    )
    assert _VALID_MODULE not in {str(path) for path in without}, (
        "the module must have left the selector's own coverage, or the "
        "exemption is not what covers it"
    )
    assert _VALID_MODULE not in uncovered(), (
        f"{_VALID_MODULE} is exempted by a verified entry, so it must not be "
        "reported as uncovered; an exemption that does not subtract is a "
        "silent pass over a module nothing executes"
    )


def test_an_exemption_whose_target_never_consumes_the_selector_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse an entry pairing a real selector with a target that ignores it.

    Both halves are real — the selector resolves to the module and the target
    exists — but the target's recipe never expands the selector, so the route
    the entry records does not exist. This is the claim the two-field
    `Exemption` exists to make checkable.
    """
    module = "tests/integration/test_workflow_integration.py"
    assert module in {
        str(path)
        for path in selected_paths(variable_expansion(SCENARIO_SELECTOR))
    }, f"{module} must be collected by {SCENARIO_SELECTOR}, or the seed is wrong"
    _seed(
        monkeypatch,
        module,
        Exemption(
            selector=SCENARIO_SELECTOR,
            target="test-python",
            reason="seeded fault: target ignores the selector",
        ),
    )
    with pytest.raises(AssertionError, match=r"does not expand"):
        exceptions_verified()


@pytest.mark.parametrize(
    ("fault", "recipe", "expected"),
    [
        (
            "commented out",
            (
                "# for p in $(foreach t,$(PYTEST_TARGETS),$(t)); do "
                "set -- $$p; $(PYTEST) $$@; done"
            ),
            "does not expand",
        ),
        (
            "echoed, not expanded",
            "echo 'for p in $(foreach t,$(PYTEST_TARGETS),$(t))'",
            "prints them",
        ),
        (
            "in a comment beside a live command",
            "echo collecting nothing # $(PYTEST_TARGETS)",
            "does not expand",
        ),
    ],
    ids=("commented-out", "echoed", "trailing-comment"),
)
def test_a_selector_only_named_in_dead_text_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    recipe: str,
    expected: str,
) -> None:
    """Refuse an exemption whose selector survives only in text the shell drops.

    Reading the recipe as a string cannot tell these from a live expansion: the
    selector's own spelling is present in all three, and the shell runs none of
    them. The first two would pass a substring test outright; the third has the
    token beside a live command but in a trailing comment, which shlex drops
    exactly as the shell does. Reading the *tokens* is what refuses them, and
    this table is what holds that behaviour to account.

    The echoed fault is refused by a *later* claim than the commented one — a
    live token that a printer consumes — so the two report differently, and
    `expected` pins which refusal each case must produce.
    """
    _seed(
        monkeypatch,
        _VALID_MODULE,
        Exemption(
            selector="PYTEST_TARGETS",
            target="test-python",
            reason=f"seeded fault: selector {fault}",
        ),
    )
    _target_recipe(monkeypatch, recipe)
    with pytest.raises(AssertionError, match=expected):
        exceptions_verified()


def test_an_exemption_no_workflow_runs_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse an entry whose target nothing in CI invokes.

    The selector and the recipe can both be right while no workflow step runs
    the target at all — the route stops inside the Makefile. Asserting the
    refusal keeps the third claim of an exemption honest, so an entry cannot
    record a route that never runs.

    The entry names the genuine suite target, so it clears the two claims
    before this one. The sweep is then narrowed to scripts that run some other
    target, which is the state a Makefile-only route would present: the recipe
    exists, and nothing in CI reaches it.
    """
    _seed(
        monkeypatch,
        _VALID_MODULE,
        Exemption(
            selector="PYTEST_TARGETS",
            target="test-python",
            reason="seeded fault: no workflow runner",
        ),
    )
    monkeypatch.setattr(
        "tests.helpers.suite_selection.run_scripts",
        lambda: [("ci.yml", "some-other-job", "0", "make test-rust")],
    )
    with pytest.raises(AssertionError, match=r"no workflow step runs"):
        exceptions_verified()


#: The Makefile target that runs the selection guard by name, and the target
#: whose prerequisites must therefore include it.
_BOOTSTRAP_TARGET = "test-selection"
_BOOTSTRAP_MODULE = "tests/test_ci_test_selection_contract.py"


def _prerequisites(target: str) -> tuple[str, ...]:
    """Return one target's declared prerequisites, in order."""
    document = makeutil_document()
    rules = typ.cast("list[dict[str, typ.Any]]", document["rules"])
    for rule in rules:
        if target in typ.cast("list[str]", rule.get("targets") or []):
            return tuple(typ.cast("list[str]", rule.get("prerequisites") or []))
    require(
        condition=False,
        message=f"the Makefile must declare a {target} target",
    )
    raise AssertionError


def test_the_bootstrap_runs_the_guard_by_name() -> None:
    """Require the route that the selector cannot silence to name the module.

    `_BOOTSTRAP_TARGET` exists because every assertion about the selector lives
    in a module the selector collects. If the bootstrap ran a *glob* or a
    directory instead of the module, a selector edit would silence it exactly
    as it silences the suite, and the bootstrap would be a no-op claiming to be
    a guard. The path is asserted literally for that reason.
    """
    recipe = recipe_of(_BOOTSTRAP_TARGET)
    assert _BOOTSTRAP_MODULE in recipe, (
        f"`make {_BOOTSTRAP_TARGET}` must name {_BOOTSTRAP_MODULE} directly, or "
        "the route that is supposed to survive a selector edit depends on the "
        f"selector too. Recipe: {recipe!r}"
    )


def test_the_python_routes_depend_on_the_bootstrap() -> None:
    """Require the suite routes to run the bootstrap rather than duplicate it.

    A target that nothing depends on is a target nobody runs. Both the local
    aggregate and the one CI invokes must list it as a prerequisite, so the
    guard is settled *before* the loop that depends on it — an ordering a
    sibling prerequisite would not give.
    """
    python = _prerequisites("test-python")
    aggregate = _prerequisites("test")
    assert _BOOTSTRAP_TARGET in python, (
        f"`make test-python` must depend on {_BOOTSTRAP_TARGET} so CI's suite "
        f"route cannot run the selector-driven loop without settling the "
        f"selector first; prerequisites: {python!r}"
    )
    assert _BOOTSTRAP_TARGET in aggregate, (
        f"`make test` must depend on {_BOOTSTRAP_TARGET} directly, not only "
        f"through `test-python`: `make test-python` overrides the selector it "
        f"reads, and the local aggregate is where a developer meets it. "
        f"Prerequisites: {aggregate!r}"
    )
