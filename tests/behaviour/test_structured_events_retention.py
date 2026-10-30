"""Behavioural coverage for bounded observation of a line-flooding child.

The line-flood scenario lives in its own module so that neither this scenario's
declaration nor its assertions pushes ``test_structured_events.py`` past the
repository's 400-line ceiling. The ``Given`` and ``When`` steps are the shared
ones, loaded here as a plugin exactly as in the companion module.
"""

from __future__ import annotations

import typing as typ

import pytest
from pytest_bdd import scenario, then

from cuprum._observability import _OBSERVE_TASK_CEILING
from tests.behaviour._structured_events_support import expected_line_events

# pytest-bdd resolves a step by looking for the fixture it created in the
# calling module's namespace, so the shared step module must be loaded here as
# well as in ``test_structured_events.py``.
pytest_plugins = ("tests.behaviour._structured_events_steps",)


@scenario(
    "../features/structured_events.feature",
    "A chatty command with capture disabled and an async observer",
)
def test_chatty_command_with_async_observer_stays_bounded() -> None:
    """Behavioural coverage for bounded retention under a line flood."""


@pytest.fixture
def behaviour_state() -> dict[str, object]:
    """Shared mutable state for this behaviour scenario.

    Returns
    -------
    dict[str, object]
        An empty mapping seeded fresh for each scenario.
    """
    return {}


@then("every line is delivered to the observer in order")
def then_every_line_delivered_in_order(behaviour_state: dict[str, object]) -> None:
    """Assert the async observer saw the exact ordered line sequence."""
    observed = typ.cast("list[str]", behaviour_state["observed_lines"])
    expected_stdout, _ = expected_line_events(behaviour_state)

    assert observed == expected_stdout, (
        f"async delivery of {len(observed)} lines differed from the "
        f"expected {len(expected_stdout)}"
    )


@then("the retained observer tasks stay bounded")
def then_retained_tasks_stay_bounded(behaviour_state: dict[str, object]) -> None:
    """Assert the run-wide pending-task list never approached the line count."""
    high_water = typ.cast("int", behaviour_state["retention_high_water"])
    line_count = typ.cast("int", behaviour_state["line_count"])

    # Without this the bound below would also pass if the spy never fired.
    assert high_water > 0, "the retention spy must have observed an emission"
    assert high_water <= _OBSERVE_TASK_CEILING, (
        f"retained tasks reached {high_water}, above the ceiling "
        f"{_OBSERVE_TASK_CEILING}"
    )
    assert high_water < line_count // 4, (
        "the retained list must stay far below the line count; "
        f"got {high_water} for {line_count} lines"
    )
