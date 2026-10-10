"""Unit tests for CuprumContext.

The context, its narrowing rules, and the scopes that apply them. Hook
registration and ordering live in ``test_context_hook_registration.py``;
the catalogue-specific behaviour lives in ``test_context_catalogue.py``.
"""

from __future__ import annotations

import logging
import typing as typ

import pytest

from cuprum.catalogue import ECHO, LS, ProgramCatalogue
from cuprum.context import (
    CuprumContext,
    ForbiddenProgramError,
    ScopeConfig,
    allow,
    current_context,
    get_context,
    scoped,
)

if typ.TYPE_CHECKING:
    from cuprum.catalogue import Program


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
