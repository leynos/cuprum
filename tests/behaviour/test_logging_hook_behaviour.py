"""Behavioural tests for the logging hook."""

from __future__ import annotations

import logging
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum import ECHO, sh
from cuprum.context import ScopeConfig, scoped
from cuprum.logging_hooks import logging_hook

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd


@scenario(
    "../features/logging_hook.feature",
    "Logging hook emits start and exit events",
)
def test_logging_hook_behaviour() -> None:
    """Behavioural coverage for logging hook integration."""


@pytest.fixture
def behaviour_state() -> dict[str, object]:
    """Shared mutable state for behaviour scenarios."""
    return {}


@given("a logging hook registered for echo")
def given_logging_hook(
    caplog: pytest.LogCaptureFixture,
    behaviour_state: dict[str, object],
) -> None:
    """Prepare a logger and record it in state."""
    caplog.set_level(logging.INFO, logger="cuprum.behaviour")
    behaviour_state["logger"] = logging.getLogger("cuprum.behaviour")


@when("I run a safe echo command")
def when_run_safe_command(
    caplog: pytest.LogCaptureFixture,
    behaviour_state: dict[str, object],
) -> None:
    """Run an allowlisted command while the logging hook is active."""
    logger = typ.cast("logging.Logger", behaviour_state["logger"])

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))), logging_hook(logger=logger):
        cmd: SafeCmd = sh.make(ECHO)("-n", "bdd")
        behaviour_state["result"] = cmd.run_sync()

    behaviour_state["logs"] = [record.getMessage() for record in caplog.records]


@then("start and exit events are logged")
def then_start_and_exit_logged(behaviour_state: dict[str, object]) -> None:
    """Verify start and exit entries appear in the captured logs."""
    logs = typ.cast("list[str]", behaviour_state["logs"])
    assert any("cuprum.start" in msg for msg in logs)
    assert any("cuprum.exit" in msg for msg in logs)
