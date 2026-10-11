"""Scoped before/after hook registration on ``CuprumContext``.

Registration is the half of the hook contract that mutates a scope: how
``before()``/``after()`` add a hook, how a registration detaches, and how it
behaves as a context manager. The order hooks actually run in is fixed here
too, because it is the property registration exists to support. The context
mechanics that are not hook-specific stay in ``test_context.py``.

Every registration test is parameterized over the before/after variants of one
structural contract, so the two hook kinds cannot drift apart.

The ``_registration`` suffix is load-bearing: pytest imports test modules by
basename, so a name this module shared with ``tests/behaviour/
test_context_hooks.py`` made a single-process collection of the whole
repository fail with ``import file mismatch``. ``make test-python`` invokes
pytest once per glob in separate processes and so could not see that; CI's
coverage job collects everything in one process and could.
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

    from cuprum._result_types import _AnyCommandResult
    from cuprum.sh import SafeCmd


class _HookRegistrationCase(typ.NamedTuple):
    """Typed variant of one before/after registration contract."""

    register: cabc.Callable[[BeforeHook | AfterHook], HookRegistration]
    hooks_attr: typ.Literal["before_hooks", "after_hooks"]
    expected_order: tuple[int, ...]


def _register_before(hook: BeforeHook | AfterHook) -> HookRegistration:
    """Adapt the before-hook factory to the shared registration case shape."""
    return before(typ.cast("BeforeHook", hook))


def _register_after(hook: BeforeHook | AfterHook) -> HookRegistration:
    """Adapt the after-hook factory to the shared registration case shape."""
    return after(typ.cast("AfterHook", hook))


#: Typed before/after variants for structurally identical registration tests.
#: The expected run order is the contract each factory exists to provide:
#: before hooks append (FIFO), after hooks prepend (LIFO).
_HOOK_REGISTRATIONS = (
    pytest.param(
        _HookRegistrationCase(_register_before, "before_hooks", (1, 2, 3)),
        id="before",
    ),
    pytest.param(
        _HookRegistrationCase(_register_after, "after_hooks", (3, 2, 1)),
        id="after",
    ),
)


def _recorder(
    case: _HookRegistrationCase,
    call_order: list[int],
    ordinal: int,
) -> BeforeHook | AfterHook:
    """Return a hook that records ``ordinal``, shaped for ``case``."""
    if case.hooks_attr == "before_hooks":

        def before_hook(cmd: SafeCmd) -> None:
            """Record this before hook's registration ordinal."""
            _ = cmd  # Unused: only the ordinal matters here.
            call_order.append(ordinal)

        return before_hook

    def after_hook(cmd: SafeCmd, result: _AnyCommandResult) -> None:
        """Record this after hook's registration ordinal."""
        _, _ = cmd, result  # Unused: only the ordinal matters here.
        call_order.append(ordinal)

    return after_hook


def _invoke(case: _HookRegistrationCase, hook: BeforeHook | AfterHook) -> None:
    """Invoke ``hook`` with the arguments its shape requires."""
    if case.hooks_attr == "before_hooks":
        typ.cast("BeforeHook", hook)(typ.cast("typ.Any", None))
    else:
        typ.cast("AfterHook", hook)(
            typ.cast("typ.Any", None), typ.cast("typ.Any", None)
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


@pytest.mark.parametrize("case", _HOOK_REGISTRATIONS)
def test_hooks_run_in_the_order_their_factory_guarantees(
    case: _HookRegistrationCase,
) -> None:
    """Registration order follows the factory: before appends, after prepends."""
    call_order: list[int] = []

    with scoped(ScopeConfig()):
        # Register through the public factories, so the ordering under test
        # is the one production code decides in with_before_hook /
        # with_after_hook rather than one this test built by hand.
        for ordinal in (1, 2, 3):
            case.register(_recorder(case, call_order, ordinal))

        registered = getattr(current_context(), case.hooks_attr)
        for hook in registered:
            _invoke(case, hook)

    assert tuple(call_order) == case.expected_order, (
        f"{case.hooks_attr} should run in {case.expected_order}"
    )
