"""Tests for the event-condition evaluator behind the runner-selection scenarios.

`tests/behaviour/test_ci_runner_selection.py` exercises the evaluator through
this repository's workflows, which between them use one `==` guard on an event
name and little else. That left the `!=` branch unexercised: inverting the
comparison passed every scenario, because no scheduled workflow depends on it.

The predicate is the mechanism, so it is driven here directly, over both
operators, both modelled fields, the `${{ }}` wrapper, and the operands this
harness deliberately declines to model.
"""

from __future__ import annotations

import pytest

from tests.helpers.ci_events import (
    OWNED_PULL_REQUEST,
    PUSH_TO_MAIN,
    TAG_PUSH,
    runs_on_event,
)


@pytest.mark.parametrize(
    ("condition", "event", "admitted"),
    [
        pytest.param(
            "github.event_name == 'pull_request'",
            OWNED_PULL_REQUEST,
            True,
            id="equal-match",
        ),
        pytest.param(
            "github.event_name == 'pull_request'",
            PUSH_TO_MAIN,
            False,
            id="equal-mismatch",
        ),
        # The case the scenarios could not reach. `rust-boundaries.yml`'s
        # `extended` job is guarded exactly this way, and inverting the
        # operator passed every scenario until this table existed.
        pytest.param(
            "github.event_name != 'pull_request'",
            PUSH_TO_MAIN,
            True,
            id="not-equal-match",
        ),
        pytest.param(
            "github.event_name != 'pull_request'",
            OWNED_PULL_REQUEST,
            False,
            id="not-equal-mismatch",
        ),
        pytest.param(
            "github.ref == 'refs/heads/main'", PUSH_TO_MAIN, True, id="ref-match"
        ),
        pytest.param(
            "github.ref == 'refs/heads/main'", TAG_PUSH, False, id="ref-mismatch"
        ),
        pytest.param(
            "${{ github.event_name == 'push' }}", PUSH_TO_MAIN, True, id="wrapped"
        ),
        pytest.param(
            "github.event_name == 'schedule' || github.event_name == 'push'",
            PUSH_TO_MAIN,
            True,
            id="alternative-admits",
        ),
        pytest.param(
            "github.event_name == 'schedule' || "
            "github.event_name == 'workflow_dispatch'",
            PUSH_TO_MAIN,
            False,
            id="no-alternative-admits",
        ),
        pytest.param(
            "github.event_name == 'push' && github.ref == 'refs/heads/main'",
            PUSH_TO_MAIN,
            True,
            id="conjunction-admits",
        ),
        pytest.param(
            "github.event_name == 'push' && github.ref == 'refs/heads/main'",
            TAG_PUSH,
            False,
            id="conjunction-refuses",
        ),
    ],
)
def test_event_conditions_are_evaluated(
    condition: str, event: dict[str, object], *, admitted: bool
) -> None:
    """Decide the comparisons this harness models, on both operators."""
    assert runs_on_event(condition, event) is admitted, (
        f"{condition!r} against {event['event_name']!r} must be "
        f"{'admitted' if admitted else 'refused'}"
    )


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param("needs.changes.outputs.bench == 'true'", id="needs-output"),
        pytest.param("inputs.workflow-harness", id="dispatch-input"),
        pytest.param("github.actor == 'dependabot[bot]'", id="actor"),
        pytest.param(
            "needs.changes.result == 'success' && (github.event_name != "
            "'pull_request' || needs.changes.outputs.bench == 'true')",
            id="parenthesized",
        ),
        pytest.param(None, id="no-condition"),
        pytest.param(True, id="non-string"),
    ],
)
def test_unmodelled_conditions_admit_the_job(condition: object) -> None:
    """Admit a job whose guard this harness does not model.

    `needs` outcomes, dispatch inputs and the actor are run-time values. A
    parenthesized expression reaches the same place by a different route: the
    terms it splits into do not match the comparison pattern either, so no
    special case is needed and none exists. Every one of them leaves the
    schedule a superset of what actually runs, which is the safe direction
    here: it can overstate what an event places, but it never claims a job is
    absent when it runs, and absence is what the scenarios assert on.
    """
    assert runs_on_event(condition, PUSH_TO_MAIN), (
        f"{condition!r} is not modelled, so the job must still be scheduled"
    )
