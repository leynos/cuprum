"""Contracts for which runs may publish the coverage ratchet baseline.

A ratchet is only a ratchet while the baseline it compares against comes from
somewhere a pull request cannot reach. Before the pinned revision the shared
action published from every run that reached its save step, so a pull request
advanced the baseline it was then measured against, and a dispatch gathering
warm-cache evidence replaced the generation it was measuring.

None of that is visible from a green run. A ratchet comparing each pull request
against itself passes exactly as one comparing against trunk does, and it goes
on passing while coverage falls.

Two facts have to hold together, and neither is sufficient alone. The action
must be pinned at or beyond the revision that added the guard, which
``GENERATE_COVERAGE`` asserts by value. And this repository's merges land
through the automerge workflow's ``GITHUB_TOKEN``, which fires no push event,
so its trunk publisher has to opt back in explicitly or the baseline stops
advancing altogether.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.ci_runners import GENERATE_COVERAGE, step_inputs, steps
from tests.helpers.ci_workflows import workflow_document

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

#: The workflow and job that publishes the trunk baseline, and the mode it
#: must ask for. ``always`` is not a preference here: see the module docstring.
TRUNK_PUBLISHER = ("coverage-main.yml", "coverage-upload")

#: The pull-request lane, which must never publish whatever the run's coverage
#: turned out to be.
PULL_REQUEST_LANE = ("ci.yml", "coverage")

#: The only ref from which the baseline may be published.
MAIN = "refs/heads/main"


def _coverage_step(workflow_name: str, job_name: str) -> Step:
    """Return the single shared-coverage step of a job."""
    matches = [
        step
        for step in steps(workflow_name, job_name)
        if step.get("uses") == GENERATE_COVERAGE
    ]
    assert len(matches) == 1, (
        f"{workflow_name}:{job_name} must invoke the coverage action exactly "
        f"once, found {len(matches)}"
    )
    return matches[0]


def _truthy(value: object) -> bool:
    """Return whether an expression operand is truthy, as Actions judges it.

    Parameters
    ----------
    value : object
        The operand to judge.

    Returns
    -------
    bool
        Whether Actions would treat it as true.
    """
    return value not in {False, "", None}


def _operand(text: str, *, event_name: str, dispatch_input: object, ref: str) -> object:
    """Evaluate one leaf of the publication expression.

    Parameters
    ----------
    text : str
        The leaf's source text.
    event_name : str
        The event the run was triggered by.
    dispatch_input : object
        The dispatch input's value, or ``""`` when the event supplies none.
    ref : str
        The ref the run was triggered against.

    Returns
    -------
    object
        The leaf's value.

    Raises
    ------
    AssertionError
        If the expression uses an operand this evaluator does not model, which
        means the contract can no longer speak to what the workflow resolves.
    """
    text = text.strip()
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    if text == "inputs.publish-baseline":
        return dispatch_input
    if text == "github.event_name == 'push'":
        return event_name == "push"
    if text == "github.ref == 'refs/heads/main'":
        return ref == "refs/heads/main"
    message = f"unsupported operand in the publication expression: {text!r}"
    raise AssertionError(message)


def _resolve_publication(
    expression: str, *, event_name: str, dispatch_input: object, ref: str
) -> object:
    """Evaluate the workflow's publish-baseline expression.

    Actions' ``&&`` and ``||`` yield an operand rather than a boolean, and
    ``&&`` binds tighter, which is what makes the ternary idiom work. Reading
    the shipped expression and evaluating it is the point: asserting its text
    would pass for an expression that reads well and resolves wrongly.

    Parameters
    ----------
    expression : str
        The single Actions expression the workflow declares.
    event_name : str
        The event the run was triggered by.
    dispatch_input : object
        The dispatch input's value, or ``""`` when the event supplies none.
    ref : str
        The ref the run was triggered against.

    Returns
    -------
    object
        The operand the expression yields, which is the mode passed to the
        action.
    """
    body = expression.strip()
    assert body.startswith("${{"), f"not an Actions expression: {expression!r}"
    assert body.endswith("}}"), f"not a single Actions expression: {expression!r}"
    body = body[3:-2].strip().replace("(", " ( ").replace(")", " ) ")
    # One bracketed group only, which the guard uses to group the disjunction.
    if "(" in body:
        before, _, rest = body.partition("(")
        grouped, _, after = rest.partition(")")
        resolved = _resolve_group(
            grouped, event_name=event_name, dispatch_input=dispatch_input, ref=ref
        )
        body = f"{before} {'TRUE' if _truthy(resolved) else 'FALSE'} {after}"
    return _resolve_group(
        body, event_name=event_name, dispatch_input=dispatch_input, ref=ref
    )


def _resolve_group(
    body: str, *, event_name: str, dispatch_input: object, ref: str
) -> object:
    """Evaluate a bracket-free ``||`` of ``&&`` chains.

    Parameters
    ----------
    body : str
        The expression body, with any bracketed group already reduced.
    event_name : str
        The event the run was triggered by.
    dispatch_input : object
        The dispatch input's value, or ``""`` when the event supplies none.
    ref : str
        The ref the run was triggered against.

    Returns
    -------
    object
        The operand the group yields.
    """
    # Actions yields the last operand it evaluated once every alternative is
    # falsey, not the first. The shipped expression ends in a literal, so no
    # case reaches that path today; getting it wrong anyway would make this
    # evaluator report a mode the workflow does not resolve to, which is
    # exactly the failure it exists to prevent.
    result: object = ""
    for alternative in body.split("||"):
        value: object = True
        for term in alternative.split("&&"):
            term = term.strip()
            if term in {"TRUE", "FALSE"}:
                candidate: object = term == "TRUE"
            else:
                candidate = _operand(
                    term,
                    event_name=event_name,
                    dispatch_input=dispatch_input,
                    ref=ref,
                )
            if not _truthy(candidate):
                value = candidate
                break
            value = candidate
        if _truthy(value):
            return value
        result = value
    return result


def _triggers(workflow_name: str) -> dict[str, object]:
    """Return a workflow's ``on:`` mapping.

    Parameters
    ----------
    workflow_name : str
        File name of the workflow.

    Returns
    -------
    dict[str, object]
        The declared triggers.
    """
    document = workflow_document(workflow_name)
    # PyYAML parses the bare `on:` key as the boolean True.
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict), "the workflow must declare an on: mapping"
    return typ.cast("dict[str, object]", triggers)


def _dispatch_input(workflow_name: str) -> dict[str, object]:
    """Return the workflow's declared ``publish-baseline`` dispatch input.

    Parameters
    ----------
    workflow_name : str
        File name of the workflow.

    Returns
    -------
    dict[str, object]
        The input's declaration.
    """
    dispatch = _triggers(workflow_name).get("workflow_dispatch")
    assert isinstance(dispatch, dict), "the dispatch trigger must declare inputs"
    inputs = dispatch["inputs"]
    assert isinstance(inputs, dict), "the dispatch trigger must declare a mapping"
    declared = inputs["publish-baseline"]
    assert isinstance(declared, dict), "publish-baseline must be a mapping"
    return typ.cast("dict[str, object]", declared)


#: Every way the trunk publisher can be reached, and what it must resolve to.
#: A dispatch defaults to publishing because carrying an automerged change is
#: the common case; a measurement run turns it off.
PUBLICATION_CASES = [
    pytest.param("push", "", MAIN, "always", id="push-to-main"),
    pytest.param("workflow_dispatch", True, MAIN, "always", id="dispatch-default"),
    pytest.param("workflow_dispatch", False, MAIN, "auto", id="dispatch-opted-out"),
    # `push` is restricted to main by its trigger, but `workflow_dispatch` is
    # not: `gh workflow run coverage-main.yml --ref some-branch` runs this job
    # against that branch. Publishing there would put a feature branch's
    # coverage into the baseline every pull request is measured against.
    pytest.param(
        "workflow_dispatch", True, "refs/heads/feature", "auto", id="dispatch-off-main"
    ),
]


@pytest.mark.parametrize(
    ("event_name", "dispatch_input", "ref", "expected"), PUBLICATION_CASES
)
def test_the_trunk_publisher_resolves_publication_per_event(
    event_name: str, dispatch_input: object, ref: str, expected: str
) -> None:
    """Each way of reaching the trunk publisher must resolve as documented.

    Its merges fire no push event, so a dispatch has to be able to publish or
    the baseline stops advancing. A dispatch is also how warm-cache evidence is
    gathered, and such a run must read the generation it is measuring rather
    than replace it, which is what the input is for.
    """
    workflow_name, job_name = TRUNK_PUBLISHER
    inputs = step_inputs(
        _coverage_step(workflow_name, job_name),
        f"{workflow_name}:{job_name} coverage must declare inputs",
    )
    expression = str(inputs.get("publish-baseline"))

    resolved = _resolve_publication(
        expression, event_name=event_name, dispatch_input=dispatch_input, ref=ref
    )

    assert resolved == expected, (
        f"{event_name} on {ref} with publish-baseline={dispatch_input!r} "
        f"resolves to {resolved!r}, expected {expected!r}; expression was "
        f"{expression!r}"
    )


def test_the_dispatch_input_defaults_to_publishing() -> None:
    """The common dispatch carries an automerged change, so it must publish."""
    workflow_name, _job_name = TRUNK_PUBLISHER
    declared = _dispatch_input(workflow_name)

    assert declared["type"] == "boolean", (
        f"publish-baseline must be a boolean, got {declared['type']!r}"
    )
    assert declared["default"] is True, (
        f"publish-baseline must default to publishing, got {declared['default']!r}"
    )
    description = declared["description"]
    assert isinstance(description, str), (
        f"publish-baseline's description must be text, got {description!r}"
    )
    assert "measurement" in description, (
        "the description must say what turning it off is for"
    )


def test_the_trunk_push_trigger_stays_on_main() -> None:
    """`always` must not become reachable from another ref.

    The expression names the push event without checking the ref, because the
    trigger already restricts it. Widening the trigger would silently widen
    publication.
    """
    workflow_name, _job_name = TRUNK_PUBLISHER
    push = _triggers(workflow_name)["push"]
    assert isinstance(push, dict), "the push trigger must declare branches"

    assert push.get("branches") == ["main"], (
        f"the push trigger must stay restricted to main, got {push.get('branches')!r}"
    )


def test_the_pull_request_lane_does_not_opt_in() -> None:
    """A pull request must not publish the baseline it is measured against.

    Leaving the input unset is what keeps it out: the action then publishes
    only on a push to ``refs/heads/main``.
    """
    workflow_name, job_name = PULL_REQUEST_LANE
    inputs = step_inputs(
        _coverage_step(workflow_name, job_name),
        f"{workflow_name}:{job_name} coverage must declare inputs",
    )

    assert "publish-baseline" not in inputs, (
        f"{workflow_name}:{job_name} sets publish-baseline="
        f"{inputs.get('publish-baseline')!r}; a pull request would then "
        f"advance the baseline it is measured against"
    )


@pytest.mark.parametrize(
    ("workflow_name", "job_name"), [TRUNK_PUBLISHER, PULL_REQUEST_LANE]
)
def test_the_ratchet_is_enabled_where_publication_is_decided(
    workflow_name: str, job_name: str
) -> None:
    """Both lanes must run the ratchet, or the guard governs nothing."""
    inputs = step_inputs(
        _coverage_step(workflow_name, job_name),
        f"{workflow_name}:{job_name} coverage must declare inputs",
    )

    assert inputs.get("with-ratchet") == "true", (
        f"{workflow_name}:{job_name} must enable the ratchet, got "
        f"{inputs.get('with-ratchet')!r}"
    )
