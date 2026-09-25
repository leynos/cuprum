"""Behavioural tests for the scoped ``env`` context manager.

These scenarios exercise the user-visible contract from issue #100: variables
written into ``os.environ`` after Cuprum is imported (the common
``monkeypatch.setenv`` case) must still be visible to subprocesses spawned
inside a ``with env(...)`` block.
"""

from __future__ import annotations

import os
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum.context import UNSET, EnvMode, env
from cuprum.sh import ExecutionContext
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


@scenario(
    "../features/env_context.feature",
    "Replace mode omits inherited variables",
)
def test_replace_mode_omits_inherited_variables() -> None:
    """Coverage for replacement semantics."""


@scenario(
    "../features/env_context.feature",
    "Explicit unset removes an inherited variable",
)
def test_explicit_unset_removes_inherited_variable() -> None:
    """Coverage for explicit deletion semantics."""


@scenario(
    "../features/env_context.feature",
    "Replace mode reaches a pipeline stage",
)
def test_replace_mode_reaches_pipeline_stage() -> None:
    """Coverage for pipeline replacement semantics."""


@pytest.fixture
def behaviour_state() -> dict[str, object]:
    """Shared mutable state for scenarios.

    Returns
    -------
    dict[str, object]
        An empty mapping that steps populate as the scenario runs.
    """
    return {}


@given(
    "a python builder available to the test catalogue",
    target_fixture="builder",
)
def given_python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    cabc.Callable[..., SafeCmd]
        A callable that builds a SafeCmd for the current interpreter.
    """
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
    assert behaviour_state["stdout"] == "scope-value", (
        'Expected behaviour_state["stdout"] == "scope-value"'
    )


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
    assert behaviour_state["stdout"] == "live-after-enter", (
        'Expected behaviour_state["stdout"] == "live-after-enter"'
    )


@when("I run a command inside a replacement env scope")
def when_run_in_replacement_scope(
    behaviour_state: dict[str, object],
    builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a command after replacing the inherited environment."""
    monkeypatch.setenv("CUPRUM_BDD_REPLACE_PARENT", "parent-value")
    command = builder(
        "-c",
        "import os;print(os.environ.get('CUPRUM_BDD_REPLACE_PARENT', '<missing>'));"
        "print(os.environ.get('CUPRUM_BDD_REPLACE_VALUE', '<missing>'))",
    )
    with env({"CUPRUM_BDD_REPLACE_VALUE": "replacement"}, mode=EnvMode.REPLACE):
        result = command.run_sync()
    behaviour_state["stdout"] = (result.stdout or "").splitlines()


@then("the subprocess receives only the replacement value")
def then_replacement_omits_parent(behaviour_state: dict[str, object]) -> None:
    """Assert replacement does not inherit the designated parent variable."""
    assert behaviour_state["stdout"] == ["<missing>", "replacement"], (
        "replacement mode must omit the inherited value and retain its own value"
    )


@when("I run a command inside an env scope explicitly unsetting CUPRUM_BDD_UNSET")
def when_run_with_explicit_unset(
    behaviour_state: dict[str, object],
    builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a command with a deletion marker over an inherited value."""
    monkeypatch.setenv("CUPRUM_BDD_UNSET", "parent-value")
    command = builder(
        "-c",
        "import os;print(os.environ.get('CUPRUM_BDD_UNSET', '<missing>'))",
    )
    with env({"CUPRUM_BDD_UNSET": UNSET}):
        result = command.run_sync()
    behaviour_state["child"] = (result.stdout or "").strip()
    behaviour_state["parent"] = os.environ.get("CUPRUM_BDD_UNSET")


@then("the subprocess does not receive CUPRUM_BDD_UNSET and the parent retains it")
def then_unset_removes_only_child_value(behaviour_state: dict[str, object]) -> None:
    """Assert explicit deletion does not mutate the test process environment."""
    assert behaviour_state["child"] == "<missing>", "UNSET must remove the child value"
    assert behaviour_state["parent"] == "parent-value", (
        "UNSET must not mutate os.environ"
    )


@when("I run a pipeline with a replacement execution context")
def when_run_pipeline_with_replacement_context(
    behaviour_state: dict[str, object],
    builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a two-stage pipeline under one per-call replacement policy."""
    monkeypatch.setenv("CUPRUM_BDD_PIPELINE_PARENT", "parent-value")
    producer = builder(
        "-c",
        "import os;print(os.environ.get('CUPRUM_BDD_PIPELINE_PARENT', '<missing>'));"
        "print(os.environ.get('CUPRUM_BDD_PIPELINE_VALUE', '<missing>'))",
    )
    consumer = builder("-c", "import sys;print(sys.stdin.read().strip())")
    result = (producer | consumer).run_sync(
        context=ExecutionContext(
            env={"CUPRUM_BDD_PIPELINE_VALUE": "replacement"},
            env_mode=EnvMode.REPLACE,
        )
    )
    behaviour_state["stdout"] = (result.stdout or "").splitlines()


@then("the pipeline stage receives only the replacement value")
def then_pipeline_stage_receives_replacement(
    behaviour_state: dict[str, object],
) -> None:
    """Assert the pipeline stage uses the same replacement policy."""
    assert behaviour_state["stdout"] == ["<missing>", "replacement"], (
        "each pipeline stage must receive the replacement environment"
    )
