"""Unit tests for CuprumContext and hooks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import typing as typ
from unittest import mock

import pytest

from cuprum.catalogue import ECHO, LS
from cuprum.context import (
    AfterHook,
    BeforeHook,
    CuprumContext,
    ForbiddenProgramError,
    after,
    allow,
    before,
    current_context,
    get_context,
    scoped,
)
from cuprum.program import Program

# =============================================================================
# CuprumContext Basics
# =============================================================================


def test_empty_context_has_no_allowlist() -> None:
    """A context without explicit allowlist has an empty frozenset."""
    ctx = CuprumContext()
    assert ctx.allowlist == frozenset()


def test_check_allowed_permits_all_with_empty_allowlist() -> None:
    """check_allowed permits all programs when allowlist is empty."""
    ctx = CuprumContext()  # Empty allowlist permits all (permissive default)
    ctx.check_allowed(ECHO)  # Must not raise
    ctx.check_allowed(LS)  # Must not raise


def test_context_with_allowlist() -> None:
    """Context retains provided allowlist."""
    programs = frozenset([ECHO, LS])
    ctx = CuprumContext(allowlist=programs)
    assert ctx.allowlist == programs


def test_is_allowed_returns_true_for_allowed_program() -> None:
    """is_allowed returns True when program is in allowlist."""
    ctx = CuprumContext(allowlist=frozenset([ECHO]))
    assert ctx.is_allowed(ECHO) is True


def test_is_allowed_returns_false_for_disallowed_program() -> None:
    """is_allowed returns False when program is not in allowlist."""
    ctx = CuprumContext(allowlist=frozenset([ECHO]))
    assert ctx.is_allowed(LS) is False


def test_empty_hooks_by_default() -> None:
    """Context has empty hooks by default."""
    ctx = CuprumContext()
    assert ctx.before_hooks == ()
    assert ctx.after_hooks == ()


def test_context_with_hooks() -> None:
    """Context retains provided hooks."""
    before_hook: BeforeHook = mock.Mock()
    after_hook: AfterHook = mock.Mock()
    ctx = CuprumContext(before_hooks=(before_hook,), after_hooks=(after_hook,))
    assert ctx.before_hooks == (before_hook,)
    assert ctx.after_hooks == (after_hook,)


# =============================================================================
# Context Narrowing
# =============================================================================


def test_narrow_reduces_allowlist() -> None:
    """narrow() intersects with parent allowlist."""
    parent = CuprumContext(allowlist=frozenset([ECHO, LS]))
    narrowed = parent.narrow(allowlist=frozenset([ECHO]))
    assert narrowed.allowlist == frozenset([ECHO])


def test_narrow_cannot_widen_allowlist() -> None:
    """narrow() cannot add programs not in parent when parent is non-empty."""
    new_program = Program("cat")
    parent = CuprumContext(allowlist=frozenset([ECHO]))
    narrowed = parent.narrow(allowlist=frozenset([ECHO, new_program]))
    assert narrowed.allowlist == frozenset([ECHO])


def test_narrow_establishes_base_when_parent_empty() -> None:
    """narrow() uses provided allowlist when parent is empty."""
    parent = CuprumContext()  # Empty allowlist
    narrowed = parent.narrow(allowlist=frozenset([ECHO, LS]))
    assert narrowed.allowlist == frozenset([ECHO, LS])


def test_narrow_appends_before_hooks() -> None:
    """narrow() appends new before hooks after parent hooks."""
    parent_hook: BeforeHook = mock.Mock()
    child_hook: BeforeHook = mock.Mock()
    parent = CuprumContext(before_hooks=(parent_hook,))
    narrowed = parent.narrow(before_hooks=(child_hook,))
    assert narrowed.before_hooks == (parent_hook, child_hook)


def test_narrow_prepends_after_hooks() -> None:
    """narrow() prepends new after hooks before parent hooks (LIFO)."""
    parent_hook: AfterHook = mock.Mock()
    child_hook: AfterHook = mock.Mock()
    parent = CuprumContext(after_hooks=(parent_hook,))
    narrowed = parent.narrow(after_hooks=(child_hook,))
    # After hooks run inner-to-outer: child first, then parent
    assert narrowed.after_hooks == (child_hook, parent_hook)


# =============================================================================
# Global ContextVar Access
# =============================================================================


def test_current_context_returns_context() -> None:
    """current_context() returns the current context."""
    ctx = current_context()
    assert isinstance(ctx, CuprumContext)


def test_get_context_returns_same_as_current() -> None:
    """get_context() is an alias for current_context()."""
    assert get_context() is current_context()


# =============================================================================
# Scoped Context Manager
# =============================================================================


def test_scoped_narrows_allowlist_in_block() -> None:
    """scoped() narrows allowlist within the context block."""
    with scoped(allowlist=frozenset([ECHO])) as ctx:
        assert ctx.is_allowed(ECHO) is True
        assert current_context() is ctx


def test_scoped_restores_context_after_block() -> None:
    """scoped() restores previous context after exiting block."""
    original = current_context()
    with scoped(allowlist=frozenset([ECHO])):
        pass
    assert current_context() is original


def test_scoped_restores_on_exception() -> None:
    """scoped() restores context even when exception is raised."""
    original = current_context()
    with (
        pytest.raises(ValueError, match=r"test"),
        scoped(allowlist=frozenset([ECHO])),
    ):
        raise ValueError("test")
    assert current_context() is original


def test_nested_scopes_stack_correctly() -> None:
    """Nested scoped() calls narrow progressively."""
    with scoped(allowlist=frozenset([ECHO, LS])) as outer:
        assert outer.is_allowed(ECHO) is True
        assert outer.is_allowed(LS) is True
        with scoped(allowlist=frozenset([ECHO])) as inner:
            assert inner.is_allowed(ECHO) is True
            assert inner.is_allowed(LS) is False
        # Back to outer scope
        assert current_context().is_allowed(LS) is True


# =============================================================================
# AllowRegistration
# =============================================================================


def test_allow_adds_programs_to_context() -> None:
    """AllowRegistration adds programs to current context allowlist."""
    with scoped(allowlist=frozenset([ECHO])):
        reg = allow(LS)
        assert current_context().is_allowed(LS) is True
        reg.detach()
        # After detach, LS should no longer be allowed in current scope
        assert current_context().is_allowed(LS) is False


def test_allow_as_context_manager() -> None:
    """AllowRegistration can be used as a context manager."""
    with scoped(allowlist=frozenset([ECHO])):
        with allow(LS):
            assert current_context().is_allowed(LS) is True
        assert current_context().is_allowed(LS) is False


# =============================================================================
# HookRegistration
# =============================================================================


def test_before_hook_registration_and_detach() -> None:
    """before() registers a hook that can be detached."""
    hook: BeforeHook = mock.Mock()
    with scoped():
        reg = before(hook)
        assert hook in current_context().before_hooks
        reg.detach()
        assert hook not in current_context().before_hooks


def test_after_hook_registration_and_detach() -> None:
    """after() registers a hook that can be detached."""
    hook: AfterHook = mock.Mock()
    with scoped():
        reg = after(hook)
        assert hook in current_context().after_hooks
        reg.detach()
        assert hook not in current_context().after_hooks


def test_before_hook_as_context_manager() -> None:
    """before() can be used as a context manager."""
    hook: BeforeHook = mock.Mock()
    with scoped():
        with before(hook):
            assert hook in current_context().before_hooks
        assert hook not in current_context().before_hooks


def test_after_hook_as_context_manager() -> None:
    """after() can be used as a context manager."""
    hook: AfterHook = mock.Mock()
    with scoped():
        with after(hook):
            assert hook in current_context().after_hooks
        assert hook not in current_context().after_hooks


# =============================================================================
# Hook Ordering
# =============================================================================


def test_before_hooks_execute_in_registration_order() -> None:
    """Before hooks execute in registration order (FIFO)."""
    call_order: list[int] = []

    def hook1(cmd: object) -> None:
        _ = cmd  # Unused
        call_order.append(1)

    def hook2(cmd: object) -> None:
        _ = cmd  # Unused
        call_order.append(2)

    def hook3(cmd: object) -> None:
        _ = cmd  # Unused
        call_order.append(3)

    ctx = CuprumContext(
        before_hooks=(
            typ.cast("BeforeHook", hook1),
            typ.cast("BeforeHook", hook2),
            typ.cast("BeforeHook", hook3),
        ),
    )

    # Execute hooks manually to verify order
    for hook in ctx.before_hooks:
        hook(typ.cast("typ.Any", None))

    assert call_order == [1, 2, 3]


def test_after_hooks_execute_in_reverse_registration_order() -> None:
    """After hooks execute inner-to-outer (LIFO within a level)."""
    call_order: list[int] = []

    def hook1(cmd: object, result: object) -> None:
        _, _ = cmd, result  # Unused
        call_order.append(1)

    def hook2(cmd: object, result: object) -> None:
        _, _ = cmd, result  # Unused
        call_order.append(2)

    def hook3(cmd: object, result: object) -> None:
        _, _ = cmd, result  # Unused
        call_order.append(3)

    # In after_hooks, prepended hooks run first
    ctx = CuprumContext(
        after_hooks=(
            typ.cast("AfterHook", hook3),
            typ.cast("AfterHook", hook2),
            typ.cast("AfterHook", hook1),
        ),
    )

    for hook in ctx.after_hooks:
        hook(typ.cast("typ.Any", None), typ.cast("typ.Any", None))

    assert call_order == [3, 2, 1]


# =============================================================================
# Context Isolation (Threads)
# =============================================================================


def test_context_is_isolated_per_thread() -> None:
    """Each thread has its own context."""
    results: dict[str, bool] = {}

    def thread_worker(name: str, programs: frozenset[Program]) -> None:
        with scoped(allowlist=programs):
            results[name] = current_context().is_allowed(ECHO)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(thread_worker, "thread1", frozenset([ECHO]))
        f2 = executor.submit(thread_worker, "thread2", frozenset([LS]))
        f1.result()
        f2.result()

    assert results["thread1"] is True
    assert results["thread2"] is False


# =============================================================================
# Context Isolation (Async Tasks)
# =============================================================================


def test_context_is_isolated_per_async_task() -> None:
    """Each async task has its own context."""
    results: dict[str, bool] = {}

    async def task_worker(name: str, programs: frozenset[Program]) -> None:
        with scoped(allowlist=programs):
            await asyncio.sleep(0.01)  # Yield to allow interleaving
            results[name] = current_context().is_allowed(ECHO)

    async def run_tasks() -> None:
        await asyncio.gather(
            task_worker("task1", frozenset([ECHO])),
            task_worker("task2", frozenset([LS])),
        )

    asyncio.run(run_tasks())

    assert results["task1"] is True
    assert results["task2"] is False


# =============================================================================
# ForbiddenProgramError
# =============================================================================


def test_forbidden_program_error_raised_for_disallowed() -> None:
    """check_allowed raises ForbiddenProgramError for disallowed programs."""
    ctx = CuprumContext(allowlist=frozenset([ECHO]))
    with pytest.raises(ForbiddenProgramError) as exc_info:
        ctx.check_allowed(LS)
    assert "ls" in str(exc_info.value).lower()


def test_check_allowed_passes_for_allowed_program() -> None:
    """check_allowed does not raise for allowed programs."""
    ctx = CuprumContext(allowlist=frozenset([ECHO]))
    ctx.check_allowed(ECHO)  # Should not raise
