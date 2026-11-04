"""Prove the suite recipe routes the selector's values into pytest.

`tests/test_ci_suite_wiring_contract.py` asks whether *anything* runs the
selector: which job, on which lanes. This module asks the question inside that
answer — whether the target that job invokes actually feeds the selector's
patterns to pytest, rather than mentioning `$(PYTEST_TARGETS)` somewhere the
shell never evaluates it.

The two fail differently. A wrong job is a wiring fault reported against the
workflow; a recipe that echoes the selector, iterates some other list, or
discards the arguments it expanded is a fault reported against the Makefile.
Keeping them apart keeps each message pointing at its own artefact.

The check itself is `tests.helpers.recipe_flow.require_selector_drives_pytest`,
a bounded structural walk rather than a shell interpreter, and the controls
below drive the same validator the production assertion uses — so a fault
cannot be refused here while the contract stays a substring test.
"""

from __future__ import annotations

import pytest

from tests.helpers.grouped_recipe_flow import (
    require_grouped_selector_drives_pytest,
)
from tests.helpers.makefile import recipe_of
from tests.helpers.makefile_references import variables_referencing
from tests.helpers.makeutil import makeutil_document
from tests.helpers.recipe_flow import (
    require_selector_drives_pytest,
    require_selector_is_consumed,
)
from tests.helpers.recipe_read import (
    command_program,
    command_segments,
    recipe_tokens,
)
from tests.helpers.suite_selection import SELECTOR

#: The pytest invocation the recipe must route the selector into. The variable
#: is named rather than assumed so the endpoint table can pair each step of the
#: flow with what breaks when it is lost.
PYTEST_INVOCATION = "PYTEST"
_INDIVIDUAL_SELECTOR = "PYTEST_INDIVIDUAL_TARGETS"


def _variable_record(
    variables: list[object], variable_name: str
) -> dict[object, object]:
    """Return one named Make variable record, failing with a useful message."""
    record = next(
        (
            record
            for record in variables
            if isinstance(record, dict) and record.get("name") == variable_name
        ),
        None,
    )
    assert isinstance(record, dict), f"Make variable {variable_name} must be assigned"
    return record


#: Each step of the data flow through the `test-python` recipe, paired with the
#: shape that satisfies every substring test while breaking the flow. Held as
#: one table because the faults are read together: a recipe can carry every
#: endpoint spelling and still collect nothing, and the controls below exist to
#: prove each fault is refused rather than described.
_RECIPE_FLOW_FAULTS = (
    (
        "the endpoints appear only as echo arguments",
        (
            "for p in $(foreach t,$(PYTEST_INDIVIDUAL_TARGETS),$(t)); do "
            "echo '$(foreach' '$(PYTEST_TARGETS)' '$(PYTEST)' '$$@'; done"
        ),
        "must invoke",
    ),
    (
        "the loop iterates a different selector",
        (
            "for p in $(foreach t,$(ACT_SCENARIO_TARGETS),$(t)); do "
            "set -- $$p; $(PYTEST) $$@; done"
        ),
        "iterates",
    ),
    (
        "the loop binds the positional parameters but pytest discards them",
        (
            "for p in $(foreach t,$(PYTEST_INDIVIDUAL_TARGETS),$(t)); do "
            "set -- $$p; $(PYTEST) -v -n 4; done"
        ),
        "does not expand",
    ),
    (
        "the endpoints sit in comments and disconnected echoes",
        (
            "# for p in $(foreach t,$(PYTEST_INDIVIDUAL_TARGETS),$(t)); do set -- $$p; "
            "$(PYTEST) $$@; done\necho collecting nothing"
        ),
        "must iterate",
    ),
    (
        "pytest runs before the loop binds anything",
        (
            "$(PYTEST) $$@; for p in $(foreach t,$(PYTEST_INDIVIDUAL_TARGETS),$(t)); "
            "do set -- $$p; done"
        ),
        "must invoke",
    ),
    (
        "the binding is printed rather than performed",
        (
            "for p in $(foreach t,$(PYTEST_INDIVIDUAL_TARGETS),$(t)); do "
            "echo set -- $$p; $(PYTEST) $$@; done"
        ),
        "must bind",
    ),
    (
        "pytest runs after the loop has finished",
        (
            "for p in $(foreach t,$(PYTEST_INDIVIDUAL_TARGETS),$(t)); do "
            "set -- $$p; done\n$(PYTEST) $$@"
        ),
        "must invoke",
    ),
    (
        "the binding is overwritten before pytest reads it",
        (
            "for p in $(foreach t,$(PYTEST_INDIVIDUAL_TARGETS),$(t)); do "
            "set -- $$p; set -- other.py; $(PYTEST) $$@; done"
        ),
        "overwrites",
    ),
)

#: The loop the real recipe uses, so a seeded fault can break one step of it
#: rather than replacing the whole shape with an unrelated one.
_LOOP_HEADER = (
    "for pattern in $(foreach target,$(PYTEST_INDIVIDUAL_TARGETS),"
    "$(call shell_quote,$(target))); do"
)


def _swap(target: str, fault: str) -> str:
    """Return the real recipe with ``target`` replaced by ``fault``."""
    return recipe_of("test-python").replace(target, fault, 1)


def test_the_suite_target_recipe_consumes_the_selector() -> None:
    """Require the recipe to route the selector's values into pytest.

    A target named `test-python` that runs a bare directory would satisfy the
    workflow check while collecting everything under `tests/` — including the
    container-bound scenarios this repository keeps out of the default suite.
    So the check pins the *data flow*, not the presence of a name: the recipe
    iterates `PYTEST_INDIVIDUAL_TARGETS`, binds each value with `set --`, and
    invokes `$(PYTEST)` over the positional parameters. A companion assertion
    ensures that list is exactly `PYTEST_TARGETS` with the grouped modules
    removed. Together they tie the workflow check to the selector the
    coverage checks read, and it is checked structurally by
    `tests.helpers.recipe_flow.require_selector_drives_pytest`, which refuses
    the faults in `_RECIPE_FLOW_FAULTS` rather than describing them.

    The helper is bounded rather than a shell interpreter: it recognizes the
    loop shape this repository's suite target is written in and refuses
    anything else by name. Refusal is the safe direction. A recipe that merely
    mentioned `PYTEST_INDIVIDUAL_TARGETS` somewhere — as an `echo` argument, in
    a comment, or in a loop that iterated something else — would satisfy a
    substring test and is reported here instead.
    """
    require_selector_drives_pytest(
        recipe_of("test-python"),
        selector=_INDIVIDUAL_SELECTOR,
        pytest_variable=PYTEST_INVOCATION,
    )


def test_individual_targets_filter_grouped_invocations() -> None:
    """Keep grouped test files out of the ordinary one-file pytest loop."""
    variables = makeutil_document().get("variables")
    assert isinstance(variables, list), "makeutil must return Makefile variables"
    individual = _variable_record(variables, _INDIVIDUAL_SELECTOR)
    assert individual.get("raw_value") == (
        f"$(filter-out $(PYTEST_GROUPED_TARGETS),$({SELECTOR}))"
    ), "individual pytest targets must exclude exactly the grouped modules"

    selected = _variable_record(variables, "PYTEST_SELECTED_TARGETS")
    assert selected.get("raw_value") == (
        "$(sort $(foreach target,$(PYTEST_TARGETS),$(wildcard $(target))))"
    ), "group selection must respect the caller's PYTEST_TARGETS selector"
    assert {
        "PYTEST_INDIVIDUAL_TARGETS",
        "PYTEST_SELECTED_TARGETS",
    } <= variables_referencing("PYTEST_TARGETS"), (
        "the test-python selection aliases must retain their PYTEST_TARGETS lineage"
    )
    recipe = recipe_of("test-python")
    require_selector_is_consumed(
        recipe,
        selector="PYTEST_GROUPED_DESELECT",
    )
    require_grouped_selector_drives_pytest(
        recipe,
        grouped_targets="PYTEST_GROUPED_TARGETS",
        selected_targets="PYTEST_SELECTED_TARGETS",
        pytest_variable=PYTEST_INVOCATION,
    )
    deselect = _variable_record(variables, "PYTEST_GROUPED_DESELECT")
    assert deselect.get("raw_value") == (
        "$(foreach target,$(PYTEST_GROUPED_TARGETS),--deselect $(target))"
    ), "every individual pytest process must deselect all grouped modules"


def test_grouped_invocation_keeps_shared_builds_on_one_worker() -> None:
    """Grouped modules share session-scoped builds, so xdist must honour the group.

    The modules named in `PYTEST_GROUPED_TARGETS` build distributions once per
    session. Under the default `load` scheduler, xdist duplicates that
    session-scoped fixture in every worker, so each worker repeats the build and
    the module's budget pays for work another worker already did. The tests carry
    an `xdist_group` marker; it only takes effect when the invocation selects
    `--dist loadgroup`.
    """
    commands = command_segments(recipe_tokens(recipe_of("test-python")))
    grouped_invocations = [
        " ".join(words)
        for _position, words in commands
        if command_program(words) == f"$({PYTEST_INVOCATION})"
        and "PYTEST_GROUPED_DESELECT" not in " ".join(words)
    ]

    assert grouped_invocations, "the grouped pytest branch must invoke pytest"
    assert any(
        "--dist loadgroup" in invocation for invocation in grouped_invocations
    ), (
        "the grouped branch must run with --dist loadgroup so xdist_group markers "
        "keep the shared distribution build on one worker"
    )


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("wrong intersection", "the grouped pytest branch must pass"),
        ("filter mentioned but dropped", "the grouped pytest branch must pass"),
        ("arguments discarded", "the grouped pytest branch must pass"),
    ],
    ids=("wrong-intersection", "filter-dropped", "arguments-discarded"),
)
def test_grouped_selector_must_reach_pytest(
    fault: str,
    expected: str,
) -> None:
    """Reject grouped recipes that name the right variables but run another set."""
    recipe = recipe_of("test-python")
    grouped_binding = (
        "set -- $(filter $(PYTEST_GROUPED_TARGETS),$(PYTEST_SELECTED_TARGETS));"
    )
    grouped_invocation = (
        '$(PYTEST) -v -n $(PYTEST_WORKERS) --dist loadgroup "$$@" || exit $$?;'
    )
    match fault:
        case "wrong intersection":
            recipe = recipe.replace(
                "$(filter $(PYTEST_GROUPED_TARGETS),$(PYTEST_SELECTED_TARGETS))",
                "$(filter-out $(PYTEST_GROUPED_TARGETS),$(PYTEST_SELECTED_TARGETS))",
                1,
            )
        case "filter mentioned but dropped":
            recipe = recipe.replace(
                grouped_binding,
                "echo $(filter $(PYTEST_GROUPED_TARGETS),$(PYTEST_SELECTED_TARGETS));",
                1,
            )
        case "arguments discarded":
            recipe = recipe.replace(
                grouped_invocation,
                "$(PYTEST) -v -n $(PYTEST_WORKERS) || exit $$?;",
                1,
            )
    assert recipe != recipe_of("test-python"), (
        f"the seeded {fault} fault must alter the recipe"
    )
    with pytest.raises(AssertionError, match=expected):
        require_grouped_selector_drives_pytest(
            recipe,
            grouped_targets="PYTEST_GROUPED_TARGETS",
            selected_targets="PYTEST_SELECTED_TARGETS",
            pytest_variable=PYTEST_INVOCATION,
        )


@pytest.mark.parametrize(
    ("fault", "recipe", "expected"),
    _RECIPE_FLOW_FAULTS,
    ids=[fault for fault, _, _ in _RECIPE_FLOW_FAULTS],
)
def test_a_recipe_that_breaks_the_flow_is_refused(
    fault: str,
    recipe: str,
    expected: str,
) -> None:
    """Show each way of satisfying the words while breaking the flow is refused.

    These are the faults a mentions-the-name check cannot see: every endpoint is
    present in the recipe's text, and the shell runs a suite that collects
    something other than what the selector names. Two of them put the words
    where the loop does not reach them — a `set --` an `echo` prints, and a
    pytest run after the loop has finished — so the body bound matters as much
    as the words. Driving them through the same validator the production
    contract uses is what makes the contract a claim about data flow; a
    per-fault bespoke assertion could agree with the contract on this table
    while the contract itself stayed a substring test.

    Each message must name the step it lost, so `expected` matches the diagnosis
    as well as the refusal: a validator that refused everything with one generic
    message would reject the estate recipe too, and the pattern match would fail
    here rather than pass.
    """
    with pytest.raises(AssertionError, match=expected):
        require_selector_drives_pytest(
            recipe,
            selector=_INDIVIDUAL_SELECTOR,
            pytest_variable=PYTEST_INVOCATION,
        )


@pytest.mark.parametrize(
    ("target", "fault"),
    [
        ("set -- $$pattern;", ""),
        ("$(PYTEST)", "echo $(PYTEST);"),
        (_LOOP_HEADER, "echo " + _LOOP_HEADER),
        ("set -- $$pattern;", "set -- $$pattern; set -- unrelated.py;"),
    ],
    ids=(
        "binding-removed",
        "pytest-not-invoked",
        "loop-disconnected",
        "binding-overwritten",
    ),
)
def test_a_single_step_removed_from_the_real_recipe_is_refused(
    target: str,
    fault: str,
) -> None:
    """Break one step of the real recipe and require the validator to notice.

    The controls above substitute whole recipes, which proves the validator
    refuses those shapes; this seeds the equivalent faults into the estate's own
    recipe, so what is refused is the recipe this repository actually runs with
    one link cut, not a synthetic approximate of it.

    Each case replaces exactly one substring, and the replacement is asserted to
    have applied. Without that guard a fault that no longer matched — after a
    legitimate recipe edit, say — would leave the recipe intact, and the test
    would pass while proving nothing about the validator.
    """
    recipe = _swap(target, fault)
    assert recipe != recipe_of("test-python"), (
        f"the seeded fault must change the recipe; {target!r} no longer appears "
        "in `make test-python`, so this control measures nothing"
    )
    with pytest.raises(AssertionError):
        require_selector_drives_pytest(
            recipe,
            selector=_INDIVIDUAL_SELECTOR,
            pytest_variable=PYTEST_INVOCATION,
        )


def test_clean_removes_retained_boundary_run_roots() -> None:
    """`make clean` removes the run evidence both boundary harnesses retain.

    Each harness keeps a copied workspace under `.cache` for diagnostics, so
    without an explicit cleanup path repeated local runs grow that directory
    without bound. The harnesses do not prune during a run: a concurrent
    invocation may be active, and the workflow archives the directory after
    its step completes.
    """
    commands = command_segments(recipe_tokens(recipe_of("clean")))
    removals = tuple(
        words for _start, words in commands if command_program(words) == "rm"
    )

    for runs in (".cache/boundary-contract/runs", ".cache/boundary-faults/runs"):
        assert any(runs in word for words in removals for word in words), (
            f"make clean must remove {runs} in an rm command the shell runs"
        )
