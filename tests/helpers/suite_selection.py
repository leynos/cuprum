"""Resolve which root-level test modules the default suite collects.

`PYTEST_TARGETS` decides what `make test` and CI's `typecheck-test` job
collect, and it is a list of glob patterns rather than a directory sweep. A
module under `tests/` that matches no pattern is absent from that suite, so a
routine `make test` on a developer's checkout never executes it: issue #499
found seven such modules at once, and pull request #488 hit the same problem
with four more.

The question "which modules does the suite collect, and which does it miss?" is
answered here, so the contract that asks it holds only the assertions. The
exception table lives beside the code that validates it rather than in the test
that consumes it: an entry is a claim about the selector, and the machinery
that checks the claim is what makes the claim worth reading.

`tests/test_ci_test_selection_contract.py` is the consumer, and its module
docstring records why the guard reads the tree and the selector rather than
the test results.
"""

from __future__ import annotations

import pathlib as pth
import typing as typ

from tests.helpers.ci_run_scripts import run_scripts
from tests.helpers.docs import repo_root
from tests.helpers.makefile import selected_paths, variable_expansion
from tests.helpers.workflow_shell import script_runs_command

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "EXCEPTIONS",
    "ROOT_MODULE_GLOB",
    "SCENARIO_SELECTOR",
    "SELECTOR",
    "covered_modules",
    "exceptions_verified",
    "remedy",
    "require",
    "root_modules",
    "uncovered",
)

#: The selector `make test-python` iterates over, and the scenario selector it
#: deliberately excludes.
SELECTOR = "PYTEST_TARGETS"
SCENARIO_SELECTOR = "ACT_SCENARIO_TARGETS"

#: Root-level modules allowed to sit outside both selectors, mapped to the
#: target that does collect them and the reason the exclusion is intended.
#:
#: It is empty, and that is the contract: issue #499 removed the last seven
#: entries by renaming those modules into the `test_ci_` selector instead. An
#: entry here is a deliberate decision that a module runs *somewhere else* —
#: never a way to silence the guard. If a module is genuinely collected by
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
#: The directory the root-level rule is about. The `Path` form is what
#: `paths` resolves against, and the `str` form is what a reported path is
#: prefixed with. The two are kept together because comparing a `Path` to the
#: string — `Path("tests") == "tests"` is False — would filter out every
#: module and report all of them as uncovered.
_TESTS = "tests"
_TESTS_DIR = pth.Path(_TESTS)


def require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


def root_modules() -> tuple[str, ...]:
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
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    tests_dir = repo_root() / _TESTS
    found = tuple(
        sorted(f"{_TESTS}/{path.name}" for path in tests_dir.glob(ROOT_MODULE_GLOB))
    )
    require(
        condition=bool(found),
        message=(
            f"{tests_dir} holds no {ROOT_MODULE_GLOB}; the guard would pass "
            "vacuously, so the enumeration itself is the failure"
        ),
    )
    return found


def covered_modules() -> frozenset[str]:
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
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    selected = selected_paths(variable_expansion(SELECTOR))
    scenarios = selected_paths(variable_expansion(SCENARIO_SELECTOR))
    require(
        condition=bool(selected),
        message=f"{SELECTOR} must resolve to at least one file",
    )
    require(
        condition=bool(scenarios),
        message=f"{SCENARIO_SELECTOR} must resolve to at least one file",
    )
    covered = frozenset(
        str(path) for path in (*selected, *scenarios) if path.parent == _TESTS_DIR
    )
    require(
        condition=bool(covered),
        message=(
            f"neither {SELECTOR} nor {SCENARIO_SELECTOR} names a root-level "
            "module, so this guard could never report an uncovered one"
        ),
    )
    return covered


def exceptions_verified() -> frozenset[str]:
    """Return the exception entries that are genuinely collected elsewhere.

    An entry in `EXCEPTIONS` exempts a module from the coverage rule, so an
    unchecked entry is a way to silence the guard rather than a record of a
    decision — exactly what the table is documented not to be. Each entry is
    therefore held to the two claims it makes: the named target's own selector
    must resolve to that module, and a workflow `run:` step must invoke the
    named target. An entry failing either claim is reported by name and never
    subtracted from the uncovered set, so the module it was hiding returns to
    the failure list.

    Returns
    -------
    frozenset of str
        Modules whose exception entry was verified on both counts.

    Raises
    ------
    AssertionError
        If an entry names a target that is not a Makefile variable, if that
        target's expansion does not resolve to the entry's module, or if no
        workflow step runs the target. Reporting the entry as the failure
        keeps a bad exemption from passing silently.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    verified: set[str] = set()
    for module, (target, reason) in EXCEPTIONS.items():
        resolved = selected_paths(variable_expansion(target))
        require(
            condition=module in {str(path) for path in resolved},
            message=(
                f"{__name__}.EXCEPTIONS exempts {module} as collected by "
                f"`make {target}`, but {target} does not resolve to it. "
                f"Recorded reason: {reason!r}. An exemption whose target does "
                "not collect the module is not an exemption, it is a gap."
            ),
        )
        runners = [
            f"{workflow_name}:{job_name}"
            for workflow_name, job_name, _index, script in run_scripts()
            if script_runs_command(script, f"make {target}")
        ]
        require(
            condition=bool(runners),
            message=(
                f"{__name__}.EXCEPTIONS exempts {module} as collected by "
                f"`make {target}`, but no workflow step runs that target, so "
                f"nothing in CI collects it. Recorded reason: {reason!r}."
            ),
        )
        verified.add(module)
    return frozenset(verified)


def uncovered() -> tuple[str, ...]:
    """Return root-level modules no selector and no verified exception covers."""
    covered = covered_modules() | exceptions_verified()
    return tuple(module for module in root_modules() if module not in covered)


def remedy(modules: cabc.Iterable[str]) -> str:
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
