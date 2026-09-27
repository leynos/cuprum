"""Keep the suite selector wired to the job that actually runs it on a PR.

`tests/test_ci_test_selection_contract.py` asks whether every root-level module
is collected; this module asks the companion question that makes that one mean
something — whether anything *runs* the selector the collection is defined by.

The two are separate because they fail differently. A module outside the
selector is a missing test, reported by name against the tree. A selector no
job evaluates, a target whose recipe ignores the selector, or a suite step
gated on a lane a pull request never schedules are all wiring faults: the tree
looks correctly covered while CI collects something else, or nothing. They are
read from three different artefacts — the Makefile, the workflow, and the job's
matrix — which is why they sit together here rather than beside the tree walk.

The resolution machinery is split the way the questions are: the recipe and
selector readings come from `tests/helpers/suite_selection.py`, and the leg
evaluation that decides *whether* a step runs comes from
`tests/helpers/ci_leg_gate.py`. The reasoning is recorded under "Test
selection" in the developers' guide.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_leg_gate import pull_request_legs
from tests.helpers.ci_run_scripts import run_scripts
from tests.helpers.ci_workflows import steps
from tests.helpers.makefile import recipe_of
from tests.helpers.suite_selection import SELECTOR
from tests.helpers.workflow_shell import script_runs_command

if typ.TYPE_CHECKING:
    import pytest

    from tests.helpers.workflow_types import Step

#: The workflow, job, and target that run the Python suite for a pull request.
#: `make test` also works locally but runs the Rust suite too, which is why CI
#: calls the Python half on its own. `typecheck-test` is the job holding the
#: `Run tests` step; `lint-test` runs the formatting, lint, Markdown, and MSRV
#: checks but never the Python suite, so naming it here would assert a contract
#: that does not exist.
CI_SUITE_WORKFLOW = "ci.yml"
CI_SUITE_JOB = "typecheck-test"
CI_SUITE_TARGET = "make test-python"

#: Every endpoint the `test-python` recipe must wire together, paired with what
#: breaks when it is dropped. Held as one table because the claims are read
#: together: a recipe satisfying three of the four still discards the selector,
#: and a reader fixing a broken recipe should see every missing endpoint at
#: once rather than rediscovering the next one on the following run.
_RECIPE_ENDPOINTS = (
    (
        f"$({SELECTOR})",
        (
            "the selector is never expanded, so the recipe collects whatever "
            "its pattern argument happened to be"
        ),
    ),
    (
        "$(foreach",
        (
            "the selector is handed to a single command instead of being "
            "iterated, so the per-pattern `[ -e ]` guard and the per-pattern "
            "exit status are lost"
        ),
    ),
    (
        "$(PYTEST)",
        "the loop's arguments never reach the configured pytest invocation",
    ),
    (
        "$$@",
        "the loop's arguments are expanded and then discarded",
    ),
)


def test_ci_invokes_the_target_that_consumes_the_selector() -> None:
    """Require `ci.yml`'s `typecheck-test` job to run `make test-python` on a PR.

    Without this, the Makefile could carry a correct selector that no job ever
    evaluates: the coverage questions would all pass while CI ran something
    else, or nothing. The assertion names the job rather than accepting any
    step anywhere, and it resolves the step's guard against the job's matrix
    legs rather than reading the command text alone.

    Two things the text cannot tell apart, and this asserts. A workflow that
    ran `make test-python` on a leg excluded by the guard — the 3.13 leg, which
    sets `python-suite: false` because the coverage job already runs pytest
    there — would satisfy a substring check while the pull-request lane that
    merges collected nothing. And a step gated on the pre-release leg would
    appear to run on every pull request while the job-level `LEG_RUNS` flag
    switched it off on exactly that event; see `tests/helpers/ci_leg_gate.py`.
    Matching the command by its leading shell tokens, and requiring at least
    one admitted leg, is what makes this a claim about execution rather than
    about text.
    """
    lanes = pull_request_legs(CI_SUITE_WORKFLOW, CI_SUITE_JOB, CI_SUITE_TARGET)
    assert lanes, (
        f"no step of {CI_SUITE_WORKFLOW}:{CI_SUITE_JOB} runs "
        f"`{CI_SUITE_TARGET}` on a pull-request leg, so {SELECTOR} is never "
        "evaluated on the lane that merges and every coverage assertion is moot"
    )
    assert any(len(admitted) > 1 for _step, admitted in lanes), (
        "the suite must run on more than one pull-request leg, or a selector "
        f"break in one interpreter is unobserved; got {lanes!r}"
    )


def test_a_guarded_suite_step_is_not_counted_as_a_pull_request_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Show the lane check reads guards, not command text.

    `pull_request_legs` is what separates "the command appears in the job" from
    "the command runs on a leg a pull request schedules". The estate happens to
    satisfy it, so nothing here would notice if the guard evaluation stopped
    discriminating and every step read as running on every leg — the check
    would keep passing while certifying a lane that collects nothing.

    The seeded fault drives that difference directly: the suite step is given
    the pre-release leg's own gate, `matrix.experimental`, which no pull
    request leg of this job sets. The step still runs `make test-python`, so a
    text-only reader reports it; the lane check must report nothing. The second
    half re-gates it on a key a pull-request leg does set, and requires the
    step back — otherwise the first half would also hold for a reader that had
    simply stopped finding the command.

    The fault is seeded by replacing the step reader rather than by mutating a
    step it returned. `steps` parses the workflow on every call, so a mutation
    of one returned mapping is invisible to the next call: the first draft of
    this test did that, and its negative control passed because the fault never
    reached the code under test.
    """
    real_steps = steps(CI_SUITE_WORKFLOW, CI_SUITE_JOB)
    # Every step of this job must carry the leg flag, so the fault is seeded as
    # a full guard rather than a bare matrix clause. A bare `matrix.experimental`
    # is refused by `ungated` for the right reason — it is not a guard this job
    # may declare — and seeding it would prove that rule rather than this one.
    flag = "env.LEG_RUNS == 'true'"

    def suite_step_under(matrix_clause: str) -> list[Step]:
        """Return this job's steps with the suite step re-gated."""
        # `dict(step)` is a shallow copy, which widens each `Step` to a plain
        # mapping; the cast records that the copies still stand in for steps,
        # which is the contract this seam has to satisfy.
        faulted = typ.cast("list[Step]", [dict(step) for step in real_steps])
        suite = next(
            step
            for step in faulted
            if script_runs_command(str(step.get("run", "")), CI_SUITE_TARGET)
        )
        suite["if"] = f"{matrix_clause} && {flag}"
        return faulted

    assert pull_request_legs(CI_SUITE_WORKFLOW, CI_SUITE_JOB, CI_SUITE_TARGET), (
        "the estate must already satisfy this check, or the seeded fault is "
        "not the difference under test"
    )
    monkeypatch.setattr(
        "tests.helpers.ci_leg_gate.steps",
        lambda _workflow, _job: suite_step_under("matrix.experimental"),
    )
    assert not pull_request_legs(CI_SUITE_WORKFLOW, CI_SUITE_JOB, CI_SUITE_TARGET), (
        "a step gated on a key no pull-request leg sets must not be reported "
        "as running on one"
    )
    monkeypatch.setattr(
        "tests.helpers.ci_leg_gate.steps",
        lambda _workflow, _job: suite_step_under("matrix.python-suite"),
    )
    assert pull_request_legs(CI_SUITE_WORKFLOW, CI_SUITE_JOB, CI_SUITE_TARGET), (
        "a step gated on a key a pull-request leg does set must be reported, "
        "or the previous assertion holds for a reader that finds nothing"
    )


def test_the_suite_target_recipe_consumes_the_selector() -> None:
    """Require the recipe to feed the selector into the pytest command.

    A target named `test-python` that runs a bare directory would satisfy the
    workflow check while collecting everything under `tests/` — including the
    container-bound scenarios this repository keeps out of the default suite.
    So the check pins the data flow, not the presence of a name: the recipe
    iterates `$(foreach ... $(PYTEST_TARGETS) ...)`, and each iteration runs
    `$(PYTEST)` over the loop variable. Asserting every endpoint of that path —
    the selector supplies the loop, the loop sets a shell variable, and that
    variable reaches pytest — is what ties the workflow check to the selector
    the coverage checks read. A recipe that merely mentioned `$(PYTEST_TARGETS)`
    somewhere would pass a substring test and fail this one.

    The endpoints are read as one table rather than as a probe apiece, so a
    recipe missing two of them reports both, and a reviewer reads the required
    data flow in one place instead of reconstructing it from four assertions.
    """
    recipe = recipe_of("test-python")
    absent = [
        f"  {endpoint!r} is missing: {consequence}"
        for endpoint, consequence in _RECIPE_ENDPOINTS
        if endpoint not in recipe
    ]
    assert not absent, (
        "the `test-python` recipe must wire every endpoint of the selection "
        "into the pytest invocation:\n" + "\n".join(absent) + f"\nRecipe: {recipe!r}"
    )
    iterated = recipe.split("$(foreach", 1)[1].split(";", 1)[0]
    assert f"$({SELECTOR})" in iterated, (
        f"the selector must be the list `$(foreach` iterates, not merely a "
        f"variable the recipe mentions. Recipe: {recipe!r}"
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
