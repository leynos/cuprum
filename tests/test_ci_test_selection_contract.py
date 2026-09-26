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
tree and the selector rather than the test results. Two rules pin the
selection:

- every root-level `tests/test_*.py` module is named by `PYTEST_TARGETS` or by
  `ACT_SCENARIO_TARGETS`, or appears in `EXCEPTIONS` with a target and a reason
  (see "Exception table" below for why that list is empty and how to add to
  it);

- the `typecheck-test` job of `ci.yml` invokes `make test-python`, the target
  that consumes the selector, so the guard connects the selector to the job
  that actually runs it instead of stopping at the Makefile.

The resolution machinery — the exception table, and everything that validates
it — lives in `tests/helpers/suite_selection.py`, so this module holds only the
assertions. The reasoning is recorded under "Test selection" in the
developers' guide.
"""

from __future__ import annotations

import pytest

from tests.helpers.ci_run_scripts import run_scripts
from tests.helpers.makefile import recipe_of
from tests.helpers.suite_selection import (
    EXCEPTIONS,
    SELECTOR,
    covered_modules,
    exceptions_verified,
    remedy,
    require,
    root_modules,
    uncovered,
)
from tests.helpers.workflow_shell import script_runs_command

#: The workflow, job, and target that run the Python suite for a pull request.
#: `make test` also works locally but runs the Rust suite too, which is why CI
#: calls the Python half on its own. `typecheck-test` is the job holding the
#: `Run tests` step; `lint-test` runs the formatting, lint, Markdown, and MSRV
#: checks but never the Python suite, so naming it here would assert a contract
#: that does not exist.
CI_SUITE_WORKFLOW = "ci.yml"
CI_SUITE_JOB = "typecheck-test"
CI_SUITE_TARGET = "make test-python"


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
    while naming a target that does not resolve to it, which is exactly the
    claim `exceptions_verified` exists to refuse.

    The module named is one this tree really has, so the check cannot pass
    because the name was unknown; the target named is a real Makefile variable
    that resolves to a different file, so it cannot pass by being undefined
    either.
    """
    module = root_modules()[0]
    assert module in covered_modules(), (
        f"{module} must already be collected, or the seeded entry is not the "
        "fault under test"
    )
    monkeypatch.setitem(
        EXCEPTIONS, module, ("ACT_PARSER_TARGETS", "seeded fault: wrong target")
    )
    with pytest.raises(AssertionError, match=r"does not resolve to it"):
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
    covered = covered_modules()
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


def test_ci_invokes_the_target_that_consumes_the_selector() -> None:
    """Require `ci.yml`'s `typecheck-test` job to run `make test-python`.

    Without this, the Makefile could carry a correct selector that no job ever
    evaluates: the questions above would all pass while CI ran something else,
    or nothing. The assertion names the job rather than accepting any step
    anywhere — a workflow that happened to run `make test-python` on a lane
    excluded by an `if:` would otherwise satisfy it, and the guard would be
    certifying a job that never executes on a pull request. Matching the
    command by its leading shell tokens, rather than by substring, keeps a
    mention in a comment from satisfying it too.
    """
    callers = [
        f"{workflow_name}:{job_name}"
        for workflow_name, job_name, _index, script in run_scripts()
        if workflow_name == CI_SUITE_WORKFLOW
        and job_name == CI_SUITE_JOB
        and script_runs_command(script, CI_SUITE_TARGET)
    ]
    assert callers, (
        f"no step of {CI_SUITE_WORKFLOW}:{CI_SUITE_JOB} runs "
        f"`{CI_SUITE_TARGET}`, so {SELECTOR} is never evaluated on the pull "
        "request lane and every coverage assertion above is moot"
    )


def test_the_suite_target_recipe_consumes_the_selector() -> None:
    """Require the recipe to feed the selector into the pytest command.

    A target named `test-python` that runs a bare directory would satisfy the
    workflow check while collecting everything under `tests/` — including the
    container-bound scenarios this repository keeps out of the default suite.
    So the check pins the data flow, not the presence of a name: the recipe
    iterates `$(foreach ... $(PYTEST_TARGETS) ...)`, and each iteration runs
    `$(PYTEST)` over the loop variable. Asserting both endpoints — the selector
    supplies the loop, and the shell variable the loop sets reaches pytest —
    is what ties the workflow check to the selector the checks above read. A
    recipe that merely mentioned `$(PYTEST_TARGETS)` somewhere would pass a
    substring test and fail this one.
    """
    recipe = recipe_of("test-python")
    assert f"$({SELECTOR})" in recipe, (
        f"the `test-python` recipe must expand $({SELECTOR}); a recipe that "
        f"runs a directory would collect the excluded scenario modules. "
        f"Recipe: {recipe!r}"
    )
    assert "$(foreach" in recipe, (
        f"the `test-python` recipe must iterate the selector with `$(foreach` "
        f"rather than passing it to a single command, or the per-pattern "
        f"`[ -e ]` guard and the per-pattern exit status are lost. "
        f"Recipe: {recipe!r}"
    )
    assert f"$({SELECTOR})" in recipe.split("$(foreach", 1)[1].split(";", 1)[0], (
        f"the selector must be the list `$(foreach` iterates, not merely a "
        f"variable the recipe mentions. Recipe: {recipe!r}"
    )
    assert "$(PYTEST)" in recipe, (
        f"the `test-python` recipe must run the suite through $(PYTEST), so "
        f"the loop variable reaches the configured pytest invocation. "
        f"Recipe: {recipe!r}"
    )
    assert "$$@" in recipe, (
        f"the `test-python` recipe must pass the loop's arguments to pytest "
        f"through `$$@`, or the selector is expanded and then discarded. "
        f"Recipe: {recipe!r}"
    )


def test_the_guard_names_the_ci_job_that_runs_the_suite() -> None:
    """Pin the attribution, because the issue text and the tree disagreed.

    Issue #499 said `lint-test` ran the suite. It does not: `lint-test` runs
    the formatting, lint, Markdown, and MSRV checks, and the `Run tests` step
    belongs to `typecheck-test`. Both appear in `ci.yml` and both are plausible
    from the issue text alone, so the constant is asserted rather than trusted
    — a guard pointing at the wrong job would certify a lane that never
    evaluates the selector.
    """
    scripts = [
        (job_name, script)
        for workflow_name, job_name, _index, script in run_scripts()
        if workflow_name == CI_SUITE_WORKFLOW
    ]
    running = {
        job_name
        for job_name, script in scripts
        if script_runs_command(script, CI_SUITE_TARGET)
    }
    assert running == {CI_SUITE_JOB}, (
        f"{CI_SUITE_TARGET} must be run by {CI_SUITE_JOB} and no other job; "
        f"found {sorted(running)}"
    )
