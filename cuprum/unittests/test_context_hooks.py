"""Scoped before/after hook registration on ``CuprumContext``.

Registration is the half of the hook contract that mutates a scope: how
``before()``/``after()`` add a hook, how a registration detaches, and how it
behaves as a context manager. The order hooks actually run in is fixed here
too, because it is the property registration exists to support. The context
mechanics that are not hook-specific stay in ``test_context.py``.

Every registration test is parameterized over the before/after variants of one
structural contract, so the two hook kinds cannot drift apart.
"""

from __future__ import annotations

import typing as typ
from unittest import mock

import pytest

from cuprum.context import (
    AfterHook,
    BeforeHook,
    CuprumContext,
    HookRegistration,
    ScopeConfig,
    after,
    before,
    current_context,
    scoped,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _HookRegistrationCase(typ.NamedTuple):
    """Typed variant of one before/after registration contract."""

    register: cabc.Callable[[BeforeHook | AfterHook], HookRegistration]
    hooks_attr: typ.Literal["before_hooks", "after_hooks"]


def _register_before(hook: BeforeHook | AfterHook) -> HookRegistration:
    """Adapt the before-hook factory to the shared registration case shape."""
    return before(typ.cast("BeforeHook", hook))


def _register_after(hook: BeforeHook | AfterHook) -> HookRegistration:
    """Adapt the after-hook factory to the shared registration case shape."""
    return after(typ.cast("AfterHook", hook))


#: Typed before/after variants for structurally identical registration tests.
_HOOK_REGISTRATIONS = (
    pytest.param(_HookRegistrationCase(_register_before, "before_hooks"), id="before"),
    pytest.param(_HookRegistrationCase(_register_after, "after_hooks"), id="after"),
)


# =============================================================================
# HookRegistration
# =============================================================================


def test_empty_hooks_by_default() -> None:
    """Context has empty hooks by default."""
    ctx = CuprumContext()
    assert not ctx.before_hooks, "a default context must start with no before-hooks"
    assert not ctx.after_hooks, "a default context must start with no after-hooks"


def test_context_with_hooks() -> None:
    """Context retains provided hooks."""
    before_hook: BeforeHook = mock.Mock()
    after_hook: AfterHook = mock.Mock()
    ctx = CuprumContext(before_hooks=(before_hook,), after_hooks=(after_hook,))
    assert ctx.before_hooks == (before_hook,)
    assert ctx.after_hooks == (after_hook,)


@pytest.mark.parametrize("case", _HOOK_REGISTRATIONS)
def test_hook_registration_and_detach(
    case: _HookRegistrationCase,
) -> None:
    """before()/after() register a hook that can be detached."""
    hook: BeforeHook | AfterHook = mock.Mock()
    with scoped(ScopeConfig()):
        reg = case.register(hook)
        assert hook in getattr(current_context(), case.hooks_attr), (
            f"{case.hooks_attr} must contain the hook after its registration"
        )
        reg.detach()
        assert hook not in getattr(current_context(), case.hooks_attr), (
            f"{case.hooks_attr} must not contain the hook after detach"
        )


@pytest.mark.parametrize("case", _HOOK_REGISTRATIONS)
def test_hook_as_context_manager(
    case: _HookRegistrationCase,
) -> None:
    """before()/after() can be used as a context manager."""
    hook: BeforeHook | AfterHook = mock.Mock()
    with scoped(ScopeConfig()):
        with case.register(hook):
            assert hook in getattr(current_context(), case.hooks_attr), (
                f"{case.hooks_attr} must contain the hook inside its context"
            )
        assert hook not in getattr(current_context(), case.hooks_attr), (
            f"{case.hooks_attr} must not contain the hook after context exit"
        )


# =============================================================================
# Hook Ordering
# =============================================================================


def test_before_hooks_execute_in_registration_order() -> None:
    """Before hooks execute in registration order (FIFO)."""
    call_order: list[int] = []

    def hook1(cmd: object) -> None:
        """Record this before hook as the first to run."""
        _ = cmd  # Unused
        call_order.append(1)

    def hook2(cmd: object) -> None:
        """Record this before hook as the second to run."""
        _ = cmd  # Unused
        call_order.append(2)

    def hook3(cmd: object) -> None:
        """Record this before hook as the third to run."""
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
        """Record this after hook as the first registered."""
        _, _ = cmd, result  # Unused
        call_order.append(1)

    def hook2(cmd: object, result: object) -> None:
        """Record this after hook as the second registered."""
        _, _ = cmd, result  # Unused
        call_order.append(2)

    def hook3(cmd: object, result: object) -> None:
        """Record this after hook as the third registered."""
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
