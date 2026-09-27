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
    # Both stages sample their own environment rather than the consumer acting
    # as a pass-through. ``_pipeline_spawn`` merges the policy per stage, so a
    # consumer that only forwarded stdin would leave a regression confined to
    # the second stage undetected: the producer's report would stay correct.
    stage_values = _print_values(inherited, supplied)
    producer = python_builder("-c", stage_values)
    consumer = python_builder(
        "-c", f"import sys;print(sys.stdin.read().strip());{stage_values}"
    )

    result = (producer | consumer).run_sync(
        context=ExecutionContext(
            env={supplied: "stage"},
            env_mode=EnvMode.REPLACE,
        )
    )

    assert result.stdout == "<missing>|stage\n<missing>|stage\n", (
        "every pipeline stage must receive the replacement environment"
    )
    assert dict(os.environ) == parent_snapshot, (
        "pipeline execution must not mutate os.environ"
    )


def test_inherit_mode_never_escapes_an_outer_replacement_boundary(
    monkeypatch: pytest.MonkeyPatch,
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """An inherit scope keeps the outer replacement boundary it was created in.

    ``INHERIT`` records that an outer scope already chose the policy, so an
    inner inherit scope must not resurrect the environment a replacement
    boundary discarded.

    The composition rule is what this pins, not the rendering: overlay and
    inherit render identically by design, so a regression that gave ``INHERIT``
    its own rendering branch is invisible here and is caught by
    ``test_inherit_renders_exactly_as_overlay`` instead. What a rendering test
    cannot see is a composition that treated ``INHERIT`` as a fresh boundary,
    and that is exactly what this asserts against.

    Both the scoped and the per-call route are exercised, because they resolve
    the mode through different entry points.
    """
    escaped = "CUPRUM_TEST_INHERIT_ESCAPED"
    own = "CUPRUM_TEST_INHERIT_OWN"
    monkeypatch.setenv(escaped, "parent-value")
    command = python_builder("-c", _print_values(escaped, own))

    with env({"CUPRUM_TEST_INHERIT_OUTER": "outer"}, mode=EnvMode.REPLACE):
        with env({own: "scoped"}, mode=EnvMode.INHERIT):
            scoped_run = command.run_sync()
        per_call = command.run_sync(
            context=ExecutionContext(env={own: "per-call"}, env_mode=EnvMode.INHERIT)
        )

    assert scoped_run.stdout == "<missing>|scoped\n", (
        "an inherit scope inside a replacement boundary must not read the live "
        "environment that boundary discarded"
    )
    assert per_call.stdout == "<missing>|per-call\n", (
        "a per-call inherit policy inside a replacement boundary must not read "
        "the live environment that boundary discarded"
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
