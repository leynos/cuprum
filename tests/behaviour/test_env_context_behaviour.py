"""Behavioural tests for the scoped ``env`` context manager.

These scenarios exercise the user-visible contract from issue #101: variables
written into ``os.environ`` after Cuprum is imported (the common
``monkeypatch.setenv`` case) must still be visible to subprocesses spawned
inside a ``with env(...)`` block.
"""

from __future__ import annotations

import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum import env
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd


@scenario(
    "../features/env_context.feature",
    "Overlay is visible to a subprocess spawned in scope",
)
def test_env_overlay_visible_to_subprocess() -> None:
    """Coverage for overlay visibility."""


@scenario(
    "../features/env_context.feature",
    "Variables set in os.environ after entering the scope are visible",
)
def test_env_live_os_environ_visible() -> None:
    """Coverage for the live ``os.environ`` view contract."""


@pytest.fixture
def behaviour_state() -> dict[str, object]:
    """Shared mutable state for scenarios."""
    return {}


@given(
    "a python builder available to the test catalogue",
    target_fixture="builder",
)
def given_python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


@when(
    "I run a command inside an env scope overlaying CUPRUM_BDD_VAR=scope-value",
)
def when_run_in_env_scope(
    behaviour_state: dict[str, object],
    builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Execute a command inside an env overlay and capture stdout."""
    cmd = builder(
        "-c",
        "import os;print(os.environ.get('CUPRUM_BDD_VAR', '<missing>'))",
    )
    with env(CUPRUM_BDD_VAR="scope-value"):
        result = cmd.run_sync()
    behaviour_state["stdout"] = (result.stdout or "").strip()


@then("the subprocess prints scope-value for CUPRUM_BDD_VAR")
def then_subprocess_prints_scope_value(
    behaviour_state: dict[str, object],
) -> None:
    """Assert the overlay variable reached the subprocess."""
    assert behaviour_state["stdout"] == "scope-value"


@when(
    "I enter an env scope, then set CUPRUM_BDD_LIVE in os.environ, then run a command",
)
def when_set_os_environ_inside_scope(
    behaviour_state: dict[str, object],
    builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set ``CUPRUM_BDD_LIVE`` after entering the scope, then run."""
    cmd = builder(
        "-c",
        "import os;print(os.environ.get('CUPRUM_BDD_LIVE', '<missing>'))",
    )
    monkeypatch.delenv("CUPRUM_BDD_LIVE", raising=False)
    with env(CUPRUM_BDD_PRESENT="present"):
        # Mutating os.environ *after* entering the scope is the regression
        # check: a snapshotting implementation would not see this update.
        monkeypatch.setenv("CUPRUM_BDD_LIVE", "live-after-enter")
        result = cmd.run_sync()
    behaviour_state["stdout"] = (result.stdout or "").strip()


@then(
    "the subprocess prints the value that was set after entering the scope",
)
def then_subprocess_prints_live_value(
    behaviour_state: dict[str, object],
) -> None:
    """Assert the live ``os.environ`` mutation reached the subprocess."""
    assert behaviour_state["stdout"] == "live-after-enter"
