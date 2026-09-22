"""Execution tests for nested, per-call, and pipeline environment policies."""

from __future__ import annotations

import os
import typing as typ

import pytest

from cuprum.context import EnvMode, env
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
