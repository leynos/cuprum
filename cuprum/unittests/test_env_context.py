"""Unit tests for the scoped ``env`` context manager.

The tests pin down the contract spelled out in issue #101: the overlay laid on
top of :func:`os.environ` must be applied *live* at subprocess spawn time, so
that variables added to ``os.environ`` after Cuprum was imported (the typical
``pytest`` ``monkeypatch.setenv`` case) remain visible inside the scope.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import os
import typing as typ

import pytest

from cuprum.context import (
    CuprumContext,
    EnvRegistration,
    ScopeConfig,
    current_context,
    env,
    merge_env_overlays,
    resolve_env,
    scoped,
)
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    from cuprum.sh import CommandResult, SafeCmd


type ExecuteFn = cabc.Callable[[SafeCmd, dict[str, typ.Any]], CommandResult]


def _execute_async(cmd: SafeCmd, kwargs: dict[str, typ.Any]) -> CommandResult:
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: dict[str, typ.Any]) -> CommandResult:
    return cmd.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def execution_strategy(request: pytest.FixtureRequest) -> tuple[str, ExecuteFn]:
    """Provide async and sync execution strategies for parameterised tests."""
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def _print_var(var: str) -> str:
    """Return a one-liner Python program that prints ``os.environ[var]``."""
    return f"import os;print(os.environ.get({var!r}, '<missing>'))"


def test_env_overlay_is_visible_to_subprocess(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """``env(...)`` must overlay a variable on the subprocess environment."""
    _, execute = execution_strategy
    var = "CUPRUM_TEST_OVERLAY_BASIC"
    cmd = python_builder("-c", _print_var(var))

    with env(**{var: "scoped"}):
        result = execute(cmd, {})

    assert result.stdout is not None
    assert result.stdout.strip() == "scoped"
    assert os.environ.get(var) is None, "overlays must not leak into os.environ"


def test_env_overlay_does_not_snapshot_os_environ(
    python_builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Live ``os.environ`` updates inside the scope must reach the subprocess.

    This is the live-view contract from issue #101. Plumbum's ``local.env``
    snapshots ``os.environ`` at module import, which is what made
    ``GIT_AUTHOR_NAME`` invisible to ``git commit`` in the original report.
    The test enters the scope, *then* mutates ``os.environ``, and the
    subprocess must observe the mutation.
    """
    _, execute = execution_strategy
    live_var = "CUPRUM_TEST_LIVE_AFTER_ENTER"
    overlay_var = "CUPRUM_TEST_OVERLAY_LIVE"
    cmd = python_builder(
        "-c",
        f"import os;print(os.environ.get({live_var!r}, '<missing>'),"
        f"os.environ.get({overlay_var!r}, '<missing>'))",
    )

    monkeypatch.delenv(live_var, raising=False)

    with env(**{overlay_var: "from-overlay"}):
        # Mutate os.environ *after* entering the scope. A snapshot
        # implementation would miss this and report "<missing>".
        monkeypatch.setenv(live_var, "from-live-os-environ")
        result = execute(cmd, {})

    assert result.stdout is not None
    assert result.stdout.strip() == "from-live-os-environ from-overlay"


def test_per_call_env_takes_precedence_over_scoped_env(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """``ExecutionContext.env`` is the most specific layer and wins."""
    from cuprum.sh import ExecutionContext

    _, execute = execution_strategy
    var = "CUPRUM_TEST_PRECEDENCE"
    cmd = python_builder("-c", _print_var(var))

    with env(**{var: "scoped"}):
        result = execute(cmd, {"context": ExecutionContext(env={var: "per-call"})})

    assert result.stdout is not None
    assert result.stdout.strip() == "per-call"


def test_nested_env_scopes_layer_innermost_wins(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Nested ``env`` scopes layer; inner values shadow outer values."""
    _, execute = execution_strategy
    var = "CUPRUM_TEST_NESTED"
    cmd = python_builder("-c", _print_var(var))

    with env(**{var: "outer"}):
        with env(**{var: "inner"}):
            result_inner = execute(cmd, {})
        result_outer = execute(cmd, {})

    assert result_inner.stdout is not None
    assert result_outer.stdout is not None
    assert result_inner.stdout.strip() == "inner"
    assert result_outer.stdout.strip() == "outer"


def test_env_scope_restores_previous_context_on_exit() -> None:
    """Exiting an ``env`` scope must clear its overlay from the context."""
    assert current_context().env_overlay is None
    with env(CUPRUM_TEST_RESTORE="x"):
        assert (current_context().env_overlay or {}).get("CUPRUM_TEST_RESTORE") == "x"
    assert current_context().env_overlay is None


def test_env_detach_is_idempotent() -> None:
    """Detaching twice must be a no-op rather than reset the token twice."""
    reg = env(CUPRUM_TEST_DETACH="x")
    try:
        assert (current_context().env_overlay or {}).get("CUPRUM_TEST_DETACH") == "x"
    finally:
        reg.detach()
        reg.detach()  # second call must not raise

    assert current_context().env_overlay is None


def test_env_overlay_is_immutable_after_registration() -> None:
    """Mutating the input mapping must not poison the stored overlay."""
    source: dict[str, str] = {"CUPRUM_TEST_FROZEN": "before"}
    reg: EnvRegistration = env(source)
    try:
        source["CUPRUM_TEST_FROZEN"] = "after"
        assert reg.overlay is not None
        assert reg.overlay["CUPRUM_TEST_FROZEN"] == "before"
    finally:
        reg.detach()


def test_resolve_env_reads_os_environ_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``resolve_env`` reads ``os.environ`` at the call site, not earlier."""
    var = "CUPRUM_TEST_RESOLVE_LIVE"
    monkeypatch.delenv(var, raising=False)

    overlay = {"CUPRUM_TEST_RESOLVE_OVERLAY": "y"}

    monkeypatch.setenv(var, "live")
    merged = resolve_env(overlay)

    assert merged is not None
    assert merged[var] == "live"
    assert merged["CUPRUM_TEST_RESOLVE_OVERLAY"] == "y"


def test_resolve_env_returns_none_when_no_layers() -> None:
    """``resolve_env`` returns ``None`` when every layer is absent or empty.

    Callers can then pass the result straight through to ``subprocess`` APIs
    to mean *inherit the parent env*. Empty mappings are treated the same as
    ``None`` so the helper does not copy ``os.environ`` when no layer
    contributes anything.
    """
    assert resolve_env() is None
    assert resolve_env(None, None) is None
    assert resolve_env({}) is None
    assert resolve_env({}, None, {}) is None


def test_resolve_env_layers_apply_left_to_right(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later layers in ``resolve_env`` must win over earlier ones."""
    var = "CUPRUM_TEST_LAYERED"
    monkeypatch.delenv(var, raising=False)

    merged = resolve_env({var: "first"}, {var: "second"})

    assert merged is not None
    assert merged[var] == "second"


def test_scoped_env_overlay_visible_to_subprocess(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """``ScopeConfig.env_overlay`` flows through ``scoped`` to subprocesses."""
    _, execute = execution_strategy
    var = "CUPRUM_TEST_SCOPE_CONFIG"
    cmd = python_builder("-c", _print_var(var))

    with scoped(ScopeConfig(env_overlay={var: "from-scope"})):
        result = execute(cmd, {})

    assert result.stdout is not None
    assert result.stdout.strip() == "from-scope"


def test_env_overlay_visible_to_pipeline_stage(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Pipeline stages spawned inside ``env(...)`` must see the overlay."""
    var = "CUPRUM_TEST_PIPELINE_OVERLAY"
    producer = python_builder("-c", _print_var(var))
    consumer = python_builder("-c", "import sys;print(sys.stdin.read().strip())")
    pipeline = producer | consumer

    with env(**{var: "pipeline-value"}):
        result = pipeline.run_sync()

    assert result.ok
    assert result.stdout is not None
    assert result.stdout.strip() == "pipeline-value"


def test_env_merge_order_and_kwarg_precedence() -> None:
    """``env(...)`` merges mappings and kwargs with ``dict``-style semantics.

    Later positional mappings override earlier ones and any keyword arguments
    override every positional layer. The original mappings handed to the
    helper must not be mutated, since :class:`EnvRegistration` stores a
    defensive snapshot via :class:`types.MappingProxyType`.
    """
    first: dict[str, str] = {"A": "1"}
    second: dict[str, str] = {"A": "2", "B": "2"}

    with env(first, second, B="3", C="4"):
        overlay = current_context().env_overlay
        assert overlay is not None
        assert dict(overlay) == {"A": "2", "B": "3", "C": "4"}

    # Inputs must be unchanged.
    assert first == {"A": "1"}
    assert second == {"A": "2", "B": "2"}
    # Context is restored on exit.
    assert current_context().env_overlay is None


def test_merge_env_overlays_returns_immutable_snapshot_when_child_is_none() -> None:
    """``merge_env_overlays(parent, None)`` must not alias ``parent``.

    A caller passing a mutable mapping must not be able to mutate the result
    afterwards and so break the overlay immutability contract.
    """
    parent: dict[str, str] = {"A": "1"}
    merged = merge_env_overlays(parent, None)

    assert merged is not None
    assert dict(merged) == {"A": "1"}
    with pytest.raises(TypeError):
        merged["A"] = "mutated"  # type: ignore[index]

    parent["A"] = "still-mutating-source"
    assert merged["A"] == "1"


def test_cuprum_context_with_env_overlay_is_immutable_proxy() -> None:
    """``CuprumContext.env_overlay`` returns a read-only mapping proxy."""
    ctx = CuprumContext().with_env_overlay({"CUPRUM_TEST_PROXY": "v"})
    assert ctx.env_overlay is not None
    with pytest.raises(TypeError):
        ctx.env_overlay["CUPRUM_TEST_PROXY"] = "other"  # type: ignore[index]
