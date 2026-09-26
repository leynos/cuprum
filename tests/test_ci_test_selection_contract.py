"""Keep every root-level test module reachable from a suite CI runs.

`PYTEST_TARGETS` decides what `make test` and CI's `lint-test` job collect, and
it is a list of glob patterns rather than a directory sweep. A module under
`tests/` that matches no pattern therefore never runs anywhere, and the
contracts inside it can regress unnoticed: issue #499 found seven such modules
at once, and pull request #488 hit the same problem with four more.

The failure is invisible from a green run, which is why this module reads the
tree and the selector rather than the test results. Two rules pin the
selection:

- every root-level `tests/test_*.py` module is named by `PYTEST_TARGETS` or by
  `ACT_SCENARIO_TARGETS`, or appears in `EXCEPTIONS` with a target and a reason
  (see "Exception table" below for why that list is empty and how to add to
  it);

- some workflow `run:` step invokes `make test-python`, the target that
  consumes the selector, so the guard connects the selector to the workflow
  that actually runs it instead of stopping at the Makefile.

The reasoning is recorded under "Test selection" in the developers' guide.
"""

from __future__ import annotations

import pathlib as pth
import typing as typ

import pytest

from tests.helpers.ci_workflows import workflow_sources
from tests.helpers.docs import repo_root
from tests.helpers.makefile import recipe_of, selected_paths, variable_expansion
from tests.helpers.strict_yaml import load
from tests.helpers.workflow_shell import script_runs_command

#: The selector `make test-python` iterates over, and the scenario selector it
#: deliberately excludes.
SELECTOR = "PYTEST_TARGETS"
SCENARIO_SELECTOR = "ACT_SCENARIO_TARGETS"

#: The workflow job that runs the Python suite for a pull request, and the
#: target it must call. `make test` also works locally but runs the Rust suite
#: too, which is why CI calls the Python half on its own.
CI_SUITE_TARGET = "make test-python"

#: Root-level modules allowed to sit outside both selectors, mapped to the
#: target that does collect them and the reason the exclusion is intended.
#:
#: It is empty, and that is the contract: issue #499 removed the last seven
#: entries by renaming those modules into the `test_ci_` selector instead. An
#: entry here is a deliberate decision that a module runs *somewhere else* —
#: never a way to silence this test. If a module is genuinely collected by
#: another target, name that target and the workflow that runs it; if it is
#: not collected anywhere, rename it or add it to `PYTEST_TARGETS`.
#:
#: The machinery is exercised regardless of the table being empty: see
#: `test_the_exception_mechanism_reports_an_uncovered_module`, which drives it
#: with a module that is not on disk.
EXCEPTIONS: typ.Final[dict[str, tuple[str, str]]] = {}

#: The glob whose match set must be exactly the module population. Kept as a
#: single pattern so the enumeration and the selector agree by construction:
#: the rule is "a module under tests/", and this is what that means on disk.
ROOT_MODULE_GLOB = "test_*.py"
#: The directory the root-level rule is about. Compared as a `Path` rather
#: than a string: `Path("tests") == "tests"` is False, so a string comparison
#: would filter out every module and report all of them as uncovered.
_TESTS = pth.Path("tests")


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


def _root_modules() -> tuple[str, ...]:
    """Return every root-level test module, as repository-relative paths.

    Returns
    -------
    tuple of str
        Each `tests/test_*.py`, sorted.

    Raises
    ------
    AssertionError
        If the enumeration is empty. An empty population would satisfy every
        "nothing is uncovered" assertion below without reading anything, which
        is the vacuous pass this module exists to prevent.
    """
    tests_dir = repo_root() / "tests"
    found = tuple(
        sorted(f"tests/{path.name}" for path in tests_dir.glob(ROOT_MODULE_GLOB))
    )
    _require(
        condition=bool(found),
        message=(
            f"{tests_dir} holds no {ROOT_MODULE_GLOB}; the guard would pass "
            "vacuously, so the enumeration itself is the failure"
        ),
    )
    return found


def _covered_modules() -> frozenset[str]:
    """Return every root-level module the two selectors resolve to.

    Returns
    -------
    frozenset of str
        Repository-relative paths named by `PYTEST_TARGETS` or
        `ACT_SCENARIO_TARGETS`.

    Raises
    ------
    AssertionError
        If either selector resolves to nothing, or if neither names a
        root-level module. Each would make the coverage question trivially
        satisfiable, so the resolver is reported as the failure rather than
        the modules it failed to find.
    """
    selected = selected_paths(variable_expansion(SELECTOR))
    scenarios = selected_paths(variable_expansion(SCENARIO_SELECTOR))
    _require(
        condition=bool(selected),
        message=f"{SELECTOR} must resolve to at least one file",
    )
    _require(
        condition=bool(scenarios),
        message=f"{SCENARIO_SELECTOR} must resolve to at least one file",
    )
    covered = frozenset(
        str(path) for path in (*selected, *scenarios) if path.parent == _TESTS
    )
    _require(
        condition=bool(covered),
        message=(
            f"neither {SELECTOR} nor {SCENARIO_SELECTOR} names a root-level "
            "module, so this guard could never report an uncovered one"
        ),
    )
    return covered


def _uncovered() -> tuple[str, ...]:
    """Return root-level modules no selector and no exception covers."""
    covered = _covered_modules() | set(EXCEPTIONS)
    return tuple(module for module in _root_modules() if module not in covered)


def _remedy(modules: typ.Iterable[str]) -> str:
    """Return the failure message naming each module and both fixes."""
    listing = "\n".join(f"  - {module}" for module in modules)
    return (
        "these root-level modules are collected by no target the suite runs, "
        "so nothing executes them:\n"
        f"{listing}\n"
        "Fix by either renaming each module to `tests/test_ci_*.py`, which "
        f"{SELECTOR} already collects, or adding it to {SELECTOR} in the "
        "Makefile. A module that is deliberately collected elsewhere can be "
        f"listed in {__name__}.EXCEPTIONS with its target and reason instead."
    )


def test_every_root_level_module_has_a_ci_route() -> None:
    """Fail when a module under `tests/` matches no pattern the suite runs.

    This is the defect issue #499 reported: seven contract modules sat under
    `tests/`, matched neither `tests/test_ci_*.py` nor the explicitly named
    `tests/test_native_sdist.py`, and never executed in `make test` or in CI.
    Renaming them into the `test_ci_` selector was the fix; this test is what
    makes the next one fail loudly instead.
    """
    uncovered = _uncovered()
    _require(
        condition=not uncovered,
        message=_remedy(uncovered),
    )


def test_the_exception_mechanism_reports_an_uncovered_module() -> None:
    """Drive the guard with a module that has no route, and see it objected to.

    The exception table above is empty, so the loop in
    `test_every_root_level_module_has_a_ci_route` currently filters nothing and
    a broken comparison could pass unnoticed. This is the seeded fault: a
    module name that cannot be covered by any selector, which the same
    machinery must report through `_remedy`. If the guard ever stops detecting
    an uncollected module, this fails first and by name.
    """
    absent = "tests/test_a_module_that_does_not_exist.py"
    assert absent not in _root_modules(), (
        "the seeded fault must not exist on disk, or the control proves nothing"
    )
    assert absent not in _covered_modules(), (
        "the seeded fault must not be covered, or the control proves nothing"
    )
    message = _remedy((absent,))
    assert absent in message, (
        f"the failure message must name the uncovered module; got {message!r}"
    )
    assert "test_ci_" in message and SELECTOR in message, (
        "the failure message must name both fixes, so a reader is told what to "
        f"do rather than only what is wrong; got {message!r}"
    )


def test_the_seven_reported_modules_are_now_collected() -> None:
    """Pin the modules issue #499 named, so a regression is reported by name.

    The population-wide check above would also catch one of these leaving the
    selector, but only as a line in a list. Naming them keeps the issue's own
    finding legible in the test that closes it, so a reader can see the seven
    without reconstructing the report.
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
    covered = _covered_modules()
    missing = sorted(expected - covered)
    assert not missing, (
        "issue #499's seven modules must stay in the suite; these are no "
        f"longer collected: {missing}"
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
    modules = _root_modules()
    covered = _covered_modules()
    unresolved = sorted(
        module for module in modules if module not in covered and module not in EXCEPTIONS
    )
    assert not unresolved, _remedy(unresolved)
    assert len(covered) > 1, (
        "a selector that resolves to a single module cannot be reading the "
        f"patterns; got {sorted(covered)}"
    )


@pytest.mark.parametrize("module", _root_modules())
def test_each_root_module_matches_a_selector_pattern(module: str) -> None:
    """Report each uncovered module separately, so one does not hide another.

    The population check fails on the first list it builds; this one fails per
    module, which is what a contributor sees in an IDE's test tree: the module
    they added is the red one.
    """
    _require(
        condition=module in _covered_modules() or module in EXCEPTIONS,
        message=_remedy((module,)),
    )


def test_ci_invokes_the_target_that_consumes_the_selector() -> None:
    """Require a workflow to run `make test-python`.

    Without this, the Makefile could carry a correct selector that no job ever
    evaluates: the questions above would all pass while CI ran something else,
    or nothing. `make test-python` is the target `lint-test` calls; matching
    the command by its leading tokens, rather than by substring, keeps a
    mention in a comment from satisfying it.
    """
    callers: list[str] = []
    for workflow_name, source in workflow_sources():
        document = typ.cast("dict[str, typ.Any]", load(source, workflow_name))
        for job_name, job in (document.get("jobs") or {}).items():
            for step in typ.cast("dict[str, typ.Any]", job).get("steps") or []:
                script = typ.cast("dict[str, typ.Any]", step).get("run")
                if isinstance(script, str) and script_runs_command(
                    script, CI_SUITE_TARGET
                ):
                    callers.append(f"{workflow_name}:{job_name}")
    assert callers, (
        f"no workflow step runs `{CI_SUITE_TARGET}`, so {SELECTOR} is never "
        "evaluated in CI and every coverage assertion above is moot"
    )


def test_the_suite_target_recipe_consumes_the_selector() -> None:
    """Require the recipe to pass the selector to pytest.

    A target named `test-python` that runs a bare directory would satisfy the
    workflow check while collecting everything under `tests/` — including the
    container-bound scenarios this repository keeps out of the default suite.
    Asserting the recipe names the variable ties the workflow check to the
    selector the checks above read.
    """
    recipe = recipe_of("test-python")
    assert f"$({SELECTOR})" in recipe, (
        f"the `test-python` recipe must expand $({SELECTOR}); a recipe that "
        f"runs a directory would collect the excluded scenario modules. "
        f"Recipe: {recipe!r}"
    )
    assert "pytest" in recipe or "$(PYTEST)" in recipe, (
        f"the `test-python` recipe must run pytest. Recipe: {recipe!r}"
    )
