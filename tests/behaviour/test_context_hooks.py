"""Behavioural tests for CuprumContext and hooks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum.catalogue import ECHO, LS
from cuprum.context import (
    CuprumContext,
    before,
    current_context,
    scoped,
)

if typ.TYPE_CHECKING:
    from cuprum.context import BeforeHook
    from cuprum.program import Program


@scenario(
    "../features/context_hooks.feature",
    "Scoped context narrows allowlist",
)
def test_scoped_context_narrows_allowlist() -> None:
    """Behavioural coverage for allowlist narrowing."""


@scenario(
    "../features/context_hooks.feature",
    "Context restores after scope exits",
)
def test_context_restores_after_scope_exits() -> None:
    """Behavioural coverage for context restoration."""


@scenario(
    "../features/context_hooks.feature",
    "Before hooks execute in registration order",
)
def test_before_hooks_execute_in_order() -> None:
    """Behavioural coverage for before hook ordering."""


@scenario(
    "../features/context_hooks.feature",
    "After hooks execute in reverse order",
)
def test_after_hooks_execute_in_reverse() -> None:
    """Behavioural coverage for after hook ordering."""


@scenario(
    "../features/context_hooks.feature",
    "Hook registration can be detached",
)
def test_hook_registration_detach() -> None:
    """Behavioural coverage for hook detachment."""


@scenario(
    "../features/context_hooks.feature",
    "Context is isolated across threads",
)
def test_context_isolated_threads() -> None:
    """Behavioural coverage for thread isolation."""


@scenario(
    "../features/context_hooks.feature",
    "Context is isolated across async tasks",
)
def test_context_isolated_async_tasks() -> None:
    """Behavioural coverage for async task isolation."""


@pytest.fixture
def behaviour_state() -> dict[str, object]:
    """Shared mutable state for behaviour scenarios."""
    return {}


@given(
    "a context that allows echo and ls",
    target_fixture="outer_context",
)
def given_context_allows_echo_and_ls() -> CuprumContext:
    """Set up an outer context allowing both ECHO and LS."""
    return CuprumContext(allowlist=frozenset([ECHO, LS]))


@when("I enter a scoped context allowing only echo")
def when_enter_scoped_echo_only(
    behaviour_state: dict[str, object],
    outer_context: CuprumContext,
) -> None:
    """Enter a scoped context with narrowed allowlist."""
    _ = outer_context  # Required fixture, used to establish scenario context
    # Use the outer context as a base and narrow via scoped()
    with (
        scoped(allowlist=frozenset([ECHO, LS])),
        scoped(allowlist=frozenset([ECHO])) as inner,
    ):
        behaviour_state["inner_context"] = inner


@then("echo is allowed in the inner scope")
def then_echo_allowed_inner(behaviour_state: dict[str, object]) -> None:
    """Verify ECHO is allowed in inner scope."""
    inner = typ.cast("CuprumContext", behaviour_state["inner_context"])
    assert inner.is_allowed(ECHO) is True


@then("ls is not allowed in the inner scope")
def then_ls_not_allowed_inner(behaviour_state: dict[str, object]) -> None:
    """Verify LS is not allowed in inner scope."""
    inner = typ.cast("CuprumContext", behaviour_state["inner_context"])
    assert inner.is_allowed(LS) is False


@when("I enter and exit a scoped context allowing only echo")
def when_enter_exit_scoped(
    behaviour_state: dict[str, object],
    outer_context: CuprumContext,
) -> None:
    """Enter and exit a nested scope."""
    _ = outer_context  # Required fixture, used to establish scenario context
    with scoped(allowlist=frozenset([ECHO, LS])):
        behaviour_state["before_exit"] = current_context()
        with scoped(allowlist=frozenset([ECHO])):
            pass
        behaviour_state["after_exit"] = current_context()


@then("ls is allowed in the outer scope")
def then_ls_allowed_outer(behaviour_state: dict[str, object]) -> None:
    """Verify LS is allowed after exiting inner scope."""
    after_exit = typ.cast("CuprumContext", behaviour_state["after_exit"])
    assert after_exit.is_allowed(LS) is True


@given(
    "a context with multiple before hooks",
    target_fixture="multi_before_context",
)
def given_multi_before_hooks(
    behaviour_state: dict[str, object],
) -> CuprumContext:
    """Create context with multiple before hooks for ordering test."""
    call_order: list[int] = []
    behaviour_state["call_order"] = call_order

    def hook1(cmd: object) -> None:
        _ = cmd  # Unused
        call_order.append(1)

    def hook2(cmd: object) -> None:
        _ = cmd  # Unused
        call_order.append(2)

    def hook3(cmd: object) -> None:
        _ = cmd  # Unused
        call_order.append(3)

    return CuprumContext(
        before_hooks=(
            typ.cast("BeforeHook", hook1),
            typ.cast("BeforeHook", hook2),
            typ.cast("BeforeHook", hook3),
        ),
    )


@when("I invoke the before hooks")
def when_invoke_before_hooks(
    multi_before_context: CuprumContext,
) -> None:
    """Execute all before hooks."""
    for hook in multi_before_context.before_hooks:
        hook(typ.cast("typ.Any", None))


@then("they execute in registration order")
def then_before_hooks_in_order(behaviour_state: dict[str, object]) -> None:
    """Verify before hooks ran in order."""
    call_order = typ.cast("list[int]", behaviour_state["call_order"])
    assert call_order == [1, 2, 3]


@given(
    "a context with multiple after hooks",
    target_fixture="multi_after_context",
)
def given_multi_after_hooks(
    behaviour_state: dict[str, object],
) -> CuprumContext:
    """Create context with multiple after hooks for ordering test."""
    call_order: list[int] = []
    behaviour_state["after_call_order"] = call_order

    def hook1(cmd: object, result: object) -> None:
        _, _ = cmd, result  # Unused
        call_order.append(1)

    def hook2(cmd: object, result: object) -> None:
        _, _ = cmd, result  # Unused
        call_order.append(2)

    def hook3(cmd: object, result: object) -> None:
        _, _ = cmd, result  # Unused
        call_order.append(3)

    # After hooks are stored in reverse order (inner-to-outer)
    return CuprumContext(
        after_hooks=(
            typ.cast("typ.Any", hook3),
            typ.cast("typ.Any", hook2),
            typ.cast("typ.Any", hook1),
        ),
    )


@when("I invoke the after hooks")
def when_invoke_after_hooks(
    multi_after_context: CuprumContext,
) -> None:
    """Execute all after hooks."""
    for hook in multi_after_context.after_hooks:
        hook(typ.cast("typ.Any", None), typ.cast("typ.Any", None))


@then("they execute in reverse registration order")
def then_after_hooks_in_reverse(behaviour_state: dict[str, object]) -> None:
    """Verify after hooks ran in reverse order (inner first)."""
    call_order = typ.cast("list[int]", behaviour_state["after_call_order"])
    assert call_order == [3, 2, 1]


@given(
    "a context with a registered before hook",
    target_fixture="hook_context",
)
def given_context_with_hook(
    behaviour_state: dict[str, object],
) -> dict[str, object]:
    """Set up a context with a hook that can be detached."""

    def my_hook(cmd: object) -> None:
        _ = cmd  # Unused

    behaviour_state["test_hook"] = my_hook
    return {"hook": my_hook}


@when("I detach the hook registration")
def when_detach_hook(
    behaviour_state: dict[str, object],
    hook_context: dict[str, object],
) -> None:
    """Register and detach a hook."""
    hook = typ.cast("BeforeHook", hook_context["hook"])
    with scoped():
        reg = before(hook)
        behaviour_state["hook_in_context_before"] = (
            hook in current_context().before_hooks
        )
        reg.detach()
        behaviour_state["hook_in_context_after"] = (
            hook in current_context().before_hooks
        )


@then("the hook is no longer in the context")
def then_hook_not_in_context(behaviour_state: dict[str, object]) -> None:
    """Verify hook was removed after detach."""
    assert behaviour_state["hook_in_context_before"] is True
    assert behaviour_state["hook_in_context_after"] is False


@given(
    "two threads with different allowlists",
    target_fixture="thread_setup",
)
def given_two_threads_different_allowlists() -> dict[str, frozenset[Program]]:
    """Set up allowlists for two threads."""
    return {
        "thread1": frozenset([ECHO]),
        "thread2": frozenset([LS]),
    }


@when("each thread checks its allowlist")
def when_threads_check_allowlist(
    behaviour_state: dict[str, object],
    thread_setup: dict[str, frozenset[Program]],
) -> None:
    """Run threads that each set and check their context."""
    results: dict[str, tuple[bool, bool]] = {}

    def thread_worker(name: str, programs: frozenset[Program]) -> None:
        with scoped(allowlist=programs):
            ctx = current_context()
            results[name] = (ctx.is_allowed(ECHO), ctx.is_allowed(LS))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(thread_worker, "thread1", thread_setup["thread1"])
        f2 = executor.submit(thread_worker, "thread2", thread_setup["thread2"])
        f1.result()
        f2.result()

    behaviour_state["thread_results"] = results


@then("each thread sees its own allowlist")
def then_threads_see_own_allowlist(behaviour_state: dict[str, object]) -> None:
    """Verify thread isolation."""
    results = typ.cast(
        "dict[str, tuple[bool, bool]]",
        behaviour_state["thread_results"],
    )
    # thread1 allows ECHO only
    assert results["thread1"] == (True, False)
    # thread2 allows LS only
    assert results["thread2"] == (False, True)


@given(
    "two async tasks with different allowlists",
    target_fixture="async_setup",
)
def given_two_async_tasks_different_allowlists() -> dict[str, frozenset[Program]]:
    """Set up allowlists for two async tasks."""
    return {
        "task1": frozenset([ECHO]),
        "task2": frozenset([LS]),
    }


@when("each task checks its allowlist")
def when_tasks_check_allowlist(
    behaviour_state: dict[str, object],
    async_setup: dict[str, frozenset[Program]],
) -> None:
    """Run async tasks that each set and check their context."""
    results: dict[str, tuple[bool, bool]] = {}

    async def task_worker(name: str, programs: frozenset[Program]) -> None:
        with scoped(allowlist=programs):
            await asyncio.sleep(0.01)  # Allow interleaving
            ctx = current_context()
            results[name] = (ctx.is_allowed(ECHO), ctx.is_allowed(LS))

    async def run_tasks() -> None:
        await asyncio.gather(
            task_worker("task1", async_setup["task1"]),
            task_worker("task2", async_setup["task2"]),
        )

    asyncio.run(run_tasks())
    behaviour_state["async_results"] = results


@then("each task sees its own allowlist")
def then_tasks_see_own_allowlist(behaviour_state: dict[str, object]) -> None:
    """Verify async task isolation."""
    results = typ.cast(
        "dict[str, tuple[bool, bool]]",
        behaviour_state["async_results"],
    )
    # task1 allows ECHO only
    assert results["task1"] == (True, False)
    # task2 allows LS only
    assert results["task2"] == (False, True)
