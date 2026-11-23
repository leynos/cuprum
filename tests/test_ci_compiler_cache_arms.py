"""Evaluate the runner, backend, and credentials arms of one fork predicate.

`tests/test_ci_compiler_cache_backend.py` holds the text of the backend and
credentials expressions against the estate's constants. That catches a
rewording, but not the failure that matters: a well-formed expression whose
predicate differs from the one `runs-on` uses. A backend that sent owned runs
to `local` while the runner expression sent them to Ubicloud, or a credentials
guard that was not the runner predicate's complement, would still match a
constant written by hand in the test.

These contracts therefore read the predicate out of the workflow's own
`runs-on`, evaluate all three expressions under the same three truth
assignments, and assert the combined outcome: a fork or Dependabot pull
request runs hosted, selects `local`, and exports no credentials; an owned run
selects Ubicloud, `gha`, and exports the credentials.

Why this lives beside the backend module rather than inside it: the module line
budget in `AGENTS.md` is 400 lines, and the backend module already carries the
ordering, pinning, and archive rules.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.ci_expressions import (
    ExpressionError,
    arm_condition,
    evaluate,
    normalized,
    unwrap,
)
from tests.helpers.ci_placement import AUTHOR_FIELD, FORK_FIELD, placement
from tests.helpers.ci_runners import (
    CREDENTIALS_STEP,
    GHA_BACKEND_JOBS,
    step_inputs,
    steps,
)
from tests.helpers.ci_workflows import job

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.workflow_types import Step

HOSTED_LABEL = "ubuntu-latest"
OWNED_LABEL = "ubicloud-standard-2"


class Scenario(typ.NamedTuple):
    """One truth assignment and the three values it must produce.

    Attributes
    ----------
    name:
        The event class, such as ``fork`` or ``owned``.
    values:
        The identifiers the expressions read, with their assigned values.
    label:
        The runner label the assignment must select.
    backend:
        The store the assignment must select.
    credentials:
        Whether the credentials step must run.
    """

    name: str
    values: dict[str, object]
    label: str
    backend: str
    credentials: bool


#: The three events the predicate has to separate: a fork pull request, a
#: Dependabot pull request, and a run on the owned arm.
SCENARIOS: typ.Final = (
    Scenario(
        "fork",
        {FORK_FIELD: True, AUTHOR_FIELD: ""},
        HOSTED_LABEL,
        "local",
        credentials=False,
    ),
    Scenario(
        "dependabot",
        {FORK_FIELD: False, AUTHOR_FIELD: "dependabot[bot]"},
        HOSTED_LABEL,
        "local",
        credentials=False,
    ),
    Scenario(
        "owned",
        {FORK_FIELD: False, AUTHOR_FIELD: "leynos"},
        OWNED_LABEL,
        "gha",
        credentials=True,
    ),
)


#: The Actions-backend lanes a fork can reach. `coverage-upload` is not one:
#: its workflow triggers only on a push to `main` and a dispatch, so it has no
#: fork arm to evaluate and passes the literal backend `gha` instead.
FORK_REACHABLE_CASES = tuple(
    (workflow_name, job_name)
    for workflow_name, job_name in GHA_BACKEND_JOBS
    if placement(workflow_name, job_name).fork is not None
)


def _step(job_steps: list[Step], name: str) -> Step:
    """Return the uniquely named step, or fail naming the count found."""
    matches = [step for step in job_steps if str(step.get("name", "")) == name]
    assert len(matches) == 1, f"expected one {name!r} step, found {len(matches)}"
    return matches[0]


def _predicate(workflow_name: str, job_name: str) -> str:
    """Return the fork/Dependabot condition the job's `runs-on` branches on."""
    declared = job(workflow_name, job_name).get("runs-on")
    body = unwrap(declared)
    return arm_condition(body, HOSTED_LABEL, OWNED_LABEL)


def _backend_condition(workflow_name: str, job_name: str) -> str:
    """Return the condition the job's backend selection branches on."""
    setup = _step(steps(workflow_name, job_name), "Set up sccache")
    inputs = step_inputs(setup, f"{workflow_name}:{job_name} setup-sccache inputs")
    body = unwrap(inputs.get("backend"))
    return _bare(arm_condition(body, "local", "gha"))


def _bare(condition: str) -> str:
    """Strip one balanced outer pair of parentheses, if there is one."""
    text = condition.strip()
    if not (text.startswith("(") and text.endswith(")")):
        return text
    depth = 0
    for index, character in enumerate(text):
        depth += 1 if character == "(" else -1 if character == ")" else 0
        if depth == 0 and index != len(text) - 1:
            return text
    return text[1:-1].strip()


def _credentials_condition(workflow_name: str, job_name: str) -> str:
    """Return the condition the credentials step is guarded by."""
    credentials = _step(steps(workflow_name, job_name), CREDENTIALS_STEP)
    text = normalized(str(credentials.get("if", "")))
    negated = text.startswith("!(") and text.endswith(")")
    assert negated, (
        f"{workflow_name}:{job_name} credentials must be guarded by a negation, "
        f"got {text!r}"
    )
    return _bare(text.removeprefix("!(").removesuffix(")"))


def _outcome(
    workflow_name: str, job_name: str, values: cabc.Mapping[str, object]
) -> tuple[object, object, object]:
    """Evaluate the runner, backend, and credentials expressions together."""
    declared = job(workflow_name, job_name).get("runs-on")
    setup = _step(steps(workflow_name, job_name), "Set up sccache")
    backend = step_inputs(
        setup, f"{workflow_name}:{job_name} setup-sccache inputs"
    ).get("backend")
    credentials = _step(steps(workflow_name, job_name), CREDENTIALS_STEP).get("if")
    return (
        evaluate(unwrap(declared), values),
        evaluate(unwrap(backend), values),
        evaluate(unwrap(credentials), values),
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), FORK_REACHABLE_CASES)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_backend_and_credentials_follow_the_runner_predicate(
    workflow_name: str, job_name: str, scenario: Scenario
) -> None:
    """One predicate decides all three, so a swapped arm cannot pass."""
    runner_condition = _bare(_predicate(workflow_name, job_name))
    assert _backend_condition(workflow_name, job_name) == runner_condition, (
        f"{workflow_name}:{job_name} selects its backend from a different "
        f"predicate than its runner; runner {runner_condition!r}"
    )
    assert _credentials_condition(workflow_name, job_name) == runner_condition, (
        f"{workflow_name}:{job_name} guards its credentials with a different "
        f"predicate than its runner; runner {runner_condition!r}"
    )
    observed = _outcome(workflow_name, job_name, scenario.values)
    expected = (scenario.label, scenario.backend, scenario.credentials)
    assert observed == expected, (
        f"{workflow_name}:{job_name} on {scenario.name} must select "
        f"{expected!r}, got {observed!r}"
    )


def test_an_unmodelled_expression_is_refused() -> None:
    """Refuse syntax rather than silently rendering it true or false."""
    with pytest.raises(ExpressionError, match="cannot tokenize"):
        evaluate("fork + 1", {"fork": True})


def test_a_missing_identifier_is_refused() -> None:
    """Fail on a name the caller did not supply rather than treating it as empty."""
    with pytest.raises(ExpressionError, match="no value supplied"):
        evaluate("fork && 'a' || 'b'", {})


def test_a_three_arm_expression_is_refused() -> None:
    """Refuse a shape with a third arm rather than reading the wrong branch."""
    with pytest.raises(ExpressionError, match="final arm"):
        arm_condition("a && 'local' || 'gha' || 's3'", "local", "gha")


def test_actions_equality_ignores_case_and_coerces_null() -> None:
    """GitHub compares strings without case and treats null and false as equal."""
    assert evaluate(
        f"{AUTHOR_FIELD} == 'DEPENDABOT[BOT]'", {AUTHOR_FIELD: "dependabot[bot]"}
    ), "a case-insensitive comparison must match"
    assert evaluate("a == b", {"a": None, "b": False}), (
        "null and false are equal under Actions semantics"
    )
    assert not evaluate(
        f"{AUTHOR_FIELD} != 'dependabot[bot]'", {AUTHOR_FIELD: "Dependabot[Bot]"}
    ), "the negation must follow the same equality"


def test_an_empty_string_compares_equal_to_null_and_false() -> None:
    """GitHub converts an empty string to zero when the types differ."""
    assert evaluate("a == b", {"a": None, "b": ""}), "null must equal the empty string"
    assert evaluate("a == b", {"a": False, "b": ""}), (
        "false must equal the empty string"
    )


def test_numeric_equality_is_exact() -> None:
    """GitHub applies no tolerance, so adjacent integers are unequal."""
    assert not evaluate("a == b", {"a": 1000000000, "b": 1000000001}), (
        "a numeric tolerance would disagree with the expression under test"
    )


def test_an_ungrouped_alternation_before_the_arm_is_refused() -> None:
    """Without grouping, a true left operand selects a boolean, not the arm."""
    with pytest.raises(ExpressionError, match="ungrouped alternation"):
        arm_condition("fork || author == 'bot' && 'local' || 'gha'", "local", "gha")


def test_two_strings_compare_as_strings_not_numbers() -> None:
    """GitHub compares two string operands as strings."""
    assert not evaluate("a == b", {"a": "0", "b": ""}), (
        "'0' and '' are different strings, not equal numbers"
    )
