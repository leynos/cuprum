"""Execution tests for nested, per-call, and pipeline environment policies."""

from __future__ import annotations

import os
import typing as typ

import pytest

from cuprum.context import (
    CuprumContext,
    EnvMode,
    EnvRegistration,
    current_context,
    env,
)
from cuprum.sh import ExecutionContext
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def _print_values(*names: str) -> str:
    """Return a program that prints requested environment values."""
    return (
        "import os;print('|'.join(os.environ.get(name, '<missing>') for name in "
        f"{names!r}))"
    )


def test_nested_replace_discards_outer_and_per_call_wins(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Nested replacement boundaries and per-call policy retain their precedence."""
    outer = "CUPRUM_TEST_NESTED_OUTER"
    inner = "CUPRUM_TEST_NESTED_INNER"
    call = "CUPRUM_TEST_NESTED_CALL"
    command = python_builder("-c", _print_values(outer, inner, call))

    with env({outer: "outer"}, mode=EnvMode.REPLACE):
        with env({inner: "inner"}):
            nested = command.run_sync()
        per_call = command.run_sync(
            context=ExecutionContext(
                env={call: "per-call"},
                env_mode=EnvMode.REPLACE,
            )
        )

    assert nested.stdout == "outer|inner|<missing>\n", (
        "an overlay inside replacement must retain the replacement boundary"
    )
    assert per_call.stdout == "<missing>|<missing>|per-call\n", (
        "a per-call replacement must discard every ambient environment layer"
    )


def test_replace_policy_reaches_every_pipeline_stage(
    monkeypatch: pytest.MonkeyPatch,
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A pipeline uses the same replacement policy as a single command."""
    inherited = "CUPRUM_TEST_PIPELINE_INHERITED"
    supplied = "CUPRUM_TEST_PIPELINE_SUPPLIED"
    monkeypatch.setenv(inherited, "parent")
    parent_snapshot = dict(os.environ)
    producer = python_builder("-c", _print_values(inherited, supplied))
    consumer = python_builder("-c", "import sys;print(sys.stdin.read().strip())")

    result = (producer | consumer).run_sync(
        context=ExecutionContext(
            env={supplied: "stage"},
            env_mode=EnvMode.REPLACE,
        )
    )

    assert result.stdout == "<missing>|stage\n", (
        "every pipeline stage must receive the replacement environment"
    )
    assert dict(os.environ) == parent_snapshot, (
        "pipeline execution must not mutate os.environ"
    )


def test_execution_context_keeps_existing_positional_slots() -> None:
    """Adding an environment mode leaves legacy positional calls intact."""
    context = ExecutionContext(None, "legacy-cwd")

    assert context.cwd == "legacy-cwd", "cwd must retain its positional slot"
    assert context.env_mode is EnvMode.OVERLAY, (
        "new positional calls must retain the default overlay mode"
    )


def test_cuprum_context_keeps_restriction_marker_positional_slot() -> None:
    """The internal restriction marker retains its established field position."""
    is_restricted = True
    context = CuprumContext(frozenset(), (), (), (), None, None, is_restricted)

    assert context._allowlist_is_restricted is True, (
        "the legacy positional argument must remain the restriction marker"
    )
    assert context.env_mode is EnvMode.OVERLAY, (
        "the new environment mode must retain its default value"
    )


def test_env_registration_keeps_legacy_overlay_default() -> None:
    """Direct registration construction retains the overlay policy default."""
    original = current_context()
    registration = EnvRegistration({"CUPRUM_TEST_DIRECT_REGISTRATION": "value"})

    try:
        context = current_context()
        assert context.env_mode is EnvMode.OVERLAY, (
            "direct registration must retain the overlay policy default"
        )
        assert context.env_overlay == {"CUPRUM_TEST_DIRECT_REGISTRATION": "value"}, (
            "direct registration must install its supplied overlay"
        )
    finally:
        registration.detach()

    assert current_context() is original, (
        "detaching a direct registration must restore the original context"
    )
