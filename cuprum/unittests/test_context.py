"""Unit tests for CuprumContext and hooks."""

from __future__ import annotations

import logging
import typing as typ
from unittest import mock

import pytest
from hypothesis import settings

from cuprum import sh
from cuprum.catalogue import ECHO, LS, ProgramCatalogue, UnknownProgramError
from cuprum.context import (
    AfterHook,
    BeforeHook,
    CuprumContext,
    ForbiddenProgramError,
    HookRegistration,
    ScopeConfig,
    after,
    allow,
    before,
    current_context,
    get_context,
    scoped,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.catalogue import Program


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

_PROPERTY_SETTINGS = settings(derandomize=True, deadline=None, max_examples=50)

# Run the symbolic backend for these tests with:
#   uv run pytest cuprum/unittests/test_context.py -m crosshair \
#     --hypothesis-profile=crosshair

# =============================================================================
# CuprumContext Basics
# =============================================================================


def test_empty_context_has_no_allowlist() -> None:
    """A context without explicit allowlist has an empty frozenset."""
    ctx = CuprumContext()
    assert ctx.allowlist == frozenset()


@pytest.mark.parametrize(
    ("ctx", "program"),
    [
        pytest.param(
            CuprumContext(),
            ECHO,
            id="empty_allowlist_permits_echo",
        ),
        pytest.param(
            CuprumContext(),
            LS,
            id="empty_allowlist_permits_ls",
        ),
        pytest.param(
            CuprumContext(allowlist=frozenset([ECHO])),
            ECHO,
            id="restricted_allowlist_permits_allowed_program",
        ),
    ],
)
def test_check_allowed_must_not_raise(ctx: CuprumContext, program: Program) -> None:
    """check_allowed does not raise for permitted programs."""
    ctx.check_allowed(program)


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
    assert not ctx.before_hooks, "a default context must start with no before-hooks"
    assert not ctx.after_hooks, "a default context must start with no after-hooks"


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


def test_with_allowlist_non_empty_replacement_is_restricted() -> None:
    """with_allowlist() marks explicit non-empty replacements as restricted."""
    replaced = CuprumContext().with_allowlist(frozenset([ECHO]))
    emptied = replaced.without_program(ECHO)

    assert emptied.allowlist == frozenset()
    with pytest.raises(ForbiddenProgramError):
        emptied.check_allowed(ECHO)


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
    """scoped(ScopeConfig()) narrows allowlist within the context block."""
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as ctx:
        assert ctx.is_allowed(ECHO) is True
        assert current_context() is ctx


def test_scoped_catalogue_narrows_to_its_allowlist() -> None:
    """scoped(catalogue=...) derives permissions from the catalogue."""
    catalogue = ProgramCatalogue.from_programs(ECHO)
    original = current_context()

    with scoped(catalogue=catalogue) as ctx:
        assert ctx.allowlist == catalogue.allowlist, (
            "catalogue scope should copy the catalogue allowlist"
        )
        assert ctx.is_allowed(LS) is False, (
            "catalogue scope should exclude programs outside its allowlist"
        )
        assert current_context() is ctx, (
            "catalogue scope should activate its narrowed context"
        )
        ctx.check_allowed(ECHO)

    assert current_context() is original, (
        "catalogue scope should restore the previous context after normal exit"
    )


def test_scoped_catalogue_blocks_unrelated_builder_construction() -> None:
    """A catalogue scope resolves builders, so unknown programs fail early."""
    catalogue = ProgramCatalogue.from_programs(ECHO)

    with (
        scoped(catalogue=catalogue),
        pytest.raises(UnknownProgramError, match="ls"),
    ):
        sh.make(LS)


def test_scoped_catalogue_denies_a_foreign_catalogue_at_run_time() -> None:
    """A builder from another catalogue constructs, then the scope denies it."""
    scoped_catalogue = ProgramCatalogue.from_programs(ECHO)
    foreign_catalogue = ProgramCatalogue.from_programs(LS)
    original = current_context()

    with scoped(catalogue=scoped_catalogue) as ctx:
        # Construction consults the named catalogue, not the active scope.
        command = sh.make(LS, catalogue=foreign_catalogue)("--version")
        assert ctx.is_allowed(LS) is False, (
            "the scope allowlist should exclude the foreign catalogue's program"
        )
        with pytest.raises(ForbiddenProgramError, match="ls"):
            command.run_sync()

    assert current_context() is original, (
        "catalogue scope should restore the previous context after normal exit"
    )


def test_scoped_catalogue_is_exposed_on_the_active_context() -> None:
    """A catalogue scope records the catalogue sh.make resolves against."""
    catalogue = ProgramCatalogue.from_programs(ECHO)

    assert current_context().catalogue is None, (
        "the default context must not carry a catalogue"
    )

    with scoped(catalogue=catalogue) as ctx:
        assert ctx.catalogue is catalogue, (
            "catalogue scope should expose its catalogue on the derived context"
        )
        assert current_context().catalogue is catalogue, (
            "the active context should carry the scope's catalogue"
        )

    assert current_context().catalogue is None, (
        "leaving the scope should drop the catalogue"
    )


def test_scope_without_catalogue_inherits_the_active_one() -> None:
    """An allowlist-only scope leaves the inherited catalogue in place."""
    catalogue = ProgramCatalogue.from_programs(ECHO, LS)

    with (
        scoped(catalogue=catalogue),
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as inner,
    ):
        assert inner.catalogue is catalogue, (
            "a scope that names no catalogue should inherit the active one"
        )


def test_nested_catalogue_scope_replaces_and_then_restores() -> None:
    """The innermost catalogue wins, and the outer one returns on exit."""
    outer_catalogue = ProgramCatalogue.from_programs(ECHO)
    inner_catalogue = ProgramCatalogue.from_programs(LS)

    with scoped(catalogue=outer_catalogue):
        assert current_context().catalogue is outer_catalogue
        with scoped(catalogue=inner_catalogue):
            assert current_context().catalogue is inner_catalogue, (
                "the innermost catalogue scope should replace the outer one"
            )
        assert current_context().catalogue is outer_catalogue, (
            "exiting the inner scope should restore the outer catalogue"
        )


def test_allow_registration_preserves_the_scoped_catalogue() -> None:
    """Registering extra programs does not disturb the active catalogue."""
    catalogue = ProgramCatalogue.from_programs(ECHO)

    with scoped(catalogue=catalogue):
        registration = allow(LS)
        assert current_context().catalogue is catalogue, (
            "allow() should derive a context that keeps the scoped catalogue"
        )
        registration.detach()
        assert current_context().catalogue is catalogue, (
            "detaching an allow registration should keep the scoped catalogue"
        )


def test_scoped_catalogue_restores_context_after_exception() -> None:
    """scoped(catalogue=...) restores the previous context on exceptions."""
    catalogue = ProgramCatalogue.from_programs(ECHO)
    original = current_context()
    message = "catalogue scope failure"

    with (
        pytest.raises(ValueError, match=message),
        scoped(catalogue=catalogue),
    ):
        raise ValueError(message)

    assert current_context() is original, (
        "catalogue scope should restore the previous context after exceptions"
    )


def test_scoped_rejects_config_and_catalogue_together() -> None:
    """scoped() keeps its configuration sources mutually exclusive."""
    catalogue = ProgramCatalogue.from_programs(ECHO)

    with pytest.raises(TypeError, match="either config or catalogue"):
        scoped(ScopeConfig(), catalogue=catalogue)


def test_scoped_requires_config_or_catalogue() -> None:
    """scoped() rejects calls that do not establish a scope configuration."""
    with pytest.raises(TypeError, match="requires config or catalogue"):
        scoped()


def test_scoped_type_hints_resolve_at_runtime() -> None:
    """scoped() exposes both accepted configuration source types."""
    hints = typ.get_type_hints(scoped)

    assert hints["config"] == ScopeConfig | None, (
        "scoped config annotation should resolve to ScopeConfig | None"
    )
    assert hints["catalogue"] == ProgramCatalogue | None, (
        "scoped catalogue annotation should resolve to ProgramCatalogue | None"
    )


def test_scoped_restores_context_after_block() -> None:
    """scoped(ScopeConfig()) restores previous context after exiting block."""
    original = current_context()
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        pass
    assert current_context() is original


def test_scoped_restores_on_exception() -> None:
    """scoped(ScopeConfig()) restores context even when exception is raised.

    Raises
    ------
    ValueError
        Raised deliberately inside the scope to exercise restoration.
    """
    original = current_context()
    message = "test"
    with (
        pytest.raises(ValueError, match=r"test"),
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))),
    ):
        raise ValueError(message)
    assert current_context() is original


def test_nested_scopes_stack_correctly() -> None:
    """Nested scoped(ScopeConfig()) calls narrow progressively."""
    with scoped(ScopeConfig(allowlist=frozenset([ECHO, LS]))) as outer:
        assert outer.is_allowed(ECHO) is True
        assert outer.is_allowed(LS) is True
        with scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as inner:
            assert inner.is_allowed(ECHO) is True
            assert inner.is_allowed(LS) is False
        # Back to outer scope
        assert current_context().is_allowed(LS) is True


# =============================================================================
# AllowRegistration
# =============================================================================


def test_allow_adds_programs_to_context() -> None:
    """AllowRegistration adds programs to current context allowlist."""
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        reg = allow(LS)
        assert current_context().is_allowed(LS) is True
        reg.detach()
        # After detach, LS should no longer be allowed in current scope
        assert current_context().is_allowed(LS) is False


def test_allow_as_context_manager() -> None:
    """AllowRegistration can be used as a context manager."""
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        with allow(LS):
            assert current_context().is_allowed(LS) is True
        assert current_context().is_allowed(LS) is False


# =============================================================================
# HookRegistration
# =============================================================================


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


# =============================================================================
# Context Isolation (Threads)
# =============================================================================


# =============================================================================
# Context Isolation (Async Tasks)
# =============================================================================


# =============================================================================
# ForbiddenProgramError
# =============================================================================


def test_forbidden_program_error_raised_for_disallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """check_allowed raises and logs denied programs."""
    ctx = CuprumContext().narrow(ScopeConfig(allowlist=frozenset([ECHO])))

    caplog.set_level(logging.WARNING, logger="cuprum.context")

    with pytest.raises(ForbiddenProgramError) as exc_info:
        ctx.check_allowed(LS)

    assert "ls" in str(exc_info.value).lower()
    assert exc_info.value.program is LS, (
        f"expected denied program to be LS, got {exc_info.value.program!r}"
    )
    assert exc_info.value.restricted_state is True, (
        "expected restricted_state to be True for a narrowed allowlist"
    )
    records = [
        record
        for record in caplog.records
        if record.name == "cuprum.context" and record.levelno == logging.WARNING
    ]
    assert len(records) == 1
    record = typ.cast("typ.Any", records[0])
    assert "ls" in record.getMessage()
    assert "restricted_state=True" in record.getMessage()
    assert record.operation == LS
    assert record.restricted_state is True


# =============================================================================
# Timeout Validation
# =============================================================================
