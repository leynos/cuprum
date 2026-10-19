"""Regression coverage for context-manager entry-time policy derivation."""

from __future__ import annotations

import asyncio
import typing as typ
from contextlib import ExitStack

import pytest

from cuprum import sh
from cuprum.catalogue import ECHO, LS
from cuprum.context import (
    CuprumContext,
    EnvMode,
    ForbiddenProgramError,
    ScopeConfig,
    allow,
    current_context,
    scoped,
)

if typ.TYPE_CHECKING:
    from cuprum.sh import BytesCommandResult, CommandResult, SafeCmd


def test_preconstructed_manager_intersects_the_active_parent() -> None:
    """A prepared manager cannot widen a narrower context at entry."""
    manager = scoped(ScopeConfig(allowlist=frozenset([ECHO, LS])))

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))), manager as context:
        assert context.allowlist == frozenset([ECHO]), (
            "a preconstructed manager must intersect with its active parent"
        )
        with pytest.raises(ForbiddenProgramError) as error:
            context.check_allowed(LS)
        assert isinstance(error.value, PermissionError), (
            "forbidden programs must raise a PermissionError subtype"
        )

    assert current_context().allowlist == frozenset(), (
        "scope exit must restore the unrestricted top-level context"
    )


def test_manager_permitting_only_the_other_program_still_intersects() -> None:
    """A manager cannot add its program to a different parent's allowlist."""
    manager = scoped(ScopeConfig(allowlist=frozenset([LS])))

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))), manager as context:
        assert context.allowlist == frozenset(), (
            "a manager must not add its program to a different parent allowlist"
        )
        with pytest.raises(ForbiddenProgramError):
            context.check_allowed(ECHO)
        with pytest.raises(ForbiddenProgramError):
            context.check_allowed(LS)


def test_manager_reuse_derives_policy_from_each_parent() -> None:
    """Sequential and unrestricted entries each use their current parent."""
    manager = scoped(ScopeConfig(allowlist=frozenset([ECHO, LS])))
    original = current_context()

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as echo_parent:
        with manager as echo_context:
            assert echo_context.allowlist == frozenset([ECHO]), (
                "reuse under the ECHO parent must derive the ECHO intersection"
            )
        assert current_context() is echo_parent, (
            "manager exit must restore the ECHO parent"
        )

    with scoped(ScopeConfig(allowlist=frozenset([LS]))) as ls_parent:
        with manager as ls_context:
            assert ls_context.allowlist == frozenset([LS]), (
                "reuse under the LS parent must derive the LS intersection"
            )
        assert current_context() is ls_parent, "manager exit must restore the LS parent"

    with scoped(ScopeConfig()) as unrestricted_parent:
        with manager as unrestricted_context:
            assert unrestricted_context.allowlist == frozenset([ECHO, LS]), (
                "an unrestricted parent may establish the configured allowlist"
            )
        assert current_context() is unrestricted_parent, (
            "manager exit must restore its unrestricted parent"
        )

    assert current_context() is original, (
        "all sequential entries must restore the original context"
    )


def test_manager_supports_nested_reuse_and_restores_each_entry() -> None:
    """Nested entries of one manager retain LIFO restoration state."""
    manager = scoped(ScopeConfig(allowlist=frozenset([ECHO, LS])))

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as parent:
        with manager as outer_entry:
            with manager as inner_entry:
                assert inner_entry.allowlist == frozenset([ECHO]), (
                    "nested reuse must retain the active parent restriction"
                )
                with pytest.raises(ForbiddenProgramError):
                    inner_entry.check_allowed(LS)
            assert current_context() is outer_entry, (
                "inner exit must restore the outer entry"
            )
        assert current_context() is parent, (
            "outer exit must restore the original parent"
        )


def test_exit_from_another_async_context_fails_without_consuming_entry() -> None:
    """A task cannot consume a manager token entered by its parent context."""
    manager = scoped(ScopeConfig(allowlist=frozenset([ECHO])))

    async def reject_foreign_exit(active_context: CuprumContext) -> None:
        """Reject the inherited entry token without changing child state."""
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="context where it was entered"):
            manager.__exit__(None, None, None)
        assert current_context() is active_context, (
            "a foreign exit must leave the child task context unchanged"
        )

    with manager as active_context:
        asyncio.run(reject_foreign_exit(active_context))
        assert current_context() is active_context, (
            "a rejected foreign exit must leave the entering context active"
        )


def test_out_of_order_exit_rejects_without_changing_active_context() -> None:
    """An out-of-order exit leaves the stack available for correct unwind."""
    first = scoped(ScopeConfig(allowlist=frozenset([ECHO, LS])))
    second = scoped(ScopeConfig(allowlist=frozenset([ECHO])))
    original = current_context()

    def assert_first_context_is_restored(expected: CuprumContext) -> None:
        """Confirm the inner manager restored its active parent."""
        assert current_context() is expected, "unwinding must restore the first entry"

    with ExitStack() as entries:
        entries.enter_context(first)
        first_context = current_context()
        entries.callback(assert_first_context_is_restored, first_context)
        entries.enter_context(second)
        active = current_context()

        with pytest.raises(RuntimeError, match="LIFO order"):
            first.__exit__(None, None, None)

        assert current_context() is active, (
            "an out-of-order exit must not change active state"
        )

    assert current_context() is original, (
        "valid LIFO unwind must restore the original context"
    )


def test_explicit_allow_widening_is_inherited_by_preconstructed_manager() -> None:
    """An explicit allow registration remains the approved widening path."""
    manager = scoped(ScopeConfig(allowlist=frozenset([ECHO, LS])))

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        with allow(LS):
            widened = current_context()
            with manager as context:
                assert context.allowlist == frozenset([ECHO, LS]), (
                    "allow() must explicitly widen the parent before manager entry"
                )
                context.check_allowed(LS)
            assert current_context() is widened, (
                "manager exit must restore the widened context"
            )
        assert not current_context().is_allowed(LS), (
            "allow() exit must restore the original restricted allowlist"
        )


def test_entry_derives_hooks_environment_and_inherited_timeout() -> None:
    """Policy fields are composed from the parent active at entry."""
    observed: list[str] = []

    def parent_before(_command: SafeCmd) -> None:
        """Record the parent before hook."""
        observed.append("parent-before")

    def child_before(_command: SafeCmd) -> None:
        """Record the child before hook."""
        observed.append("child-before")

    def parent_after(
        _command: SafeCmd,
        _result: CommandResult | BytesCommandResult,
    ) -> None:
        """Record the parent after hook."""
        observed.append("parent-after")

    def child_after(
        _command: SafeCmd,
        _result: CommandResult | BytesCommandResult,
    ) -> None:
        """Record the child after hook."""
        observed.append("child-after")

    manager = scoped(
        ScopeConfig(
            allowlist=frozenset([ECHO, LS]),
            before_hooks=(child_before,),
            after_hooks=(child_after,),
            env_overlay={"SHARED": "child", "CHILD_ONLY": "child"},
            timeout=None,
        )
    )

    with (
        scoped(
            ScopeConfig(
                allowlist=frozenset([ECHO]),
                before_hooks=(parent_before,),
                after_hooks=(parent_after,),
                env_overlay={"SHARED": "parent", "PARENT_ONLY": "parent"},
                timeout=9.0,
            )
        ),
        manager as context,
    ):
        assert context.before_hooks == (parent_before, child_before), (
            "before hooks must inherit parent-first order at entry"
        )
        assert context.after_hooks == (child_after, parent_after), (
            "after hooks must inherit child-first order at entry"
        )
        assert dict(context.env_overlay or {}) == {
            "SHARED": "child",
            "PARENT_ONLY": "parent",
            "CHILD_ONLY": "child",
        }, "child environment values must override while inheriting parent keys"
        assert context.timeout == pytest.approx(9.0), (
            "timeout=None must inherit the active parent's timeout"
        )
        for hook in context.before_hooks:
            hook(sh.make(ECHO)("before"))
        for hook in context.after_hooks:
            hook(
                sh.make(ECHO)("after"),
                typ.cast("CommandResult | BytesCommandResult", None),
            )

    assert observed == [
        "parent-before",
        "child-before",
        "child-after",
        "parent-after",
    ], "hooks must run parent-first before and child-first after"


def test_entry_applies_child_replace_mode_and_timeout_override() -> None:
    """Child replacement and explicit timeout override inherited policy."""
    manager = scoped(
        ScopeConfig(
            env_overlay={"CHILD_ONLY": "child"},
            env_mode=EnvMode.REPLACE,
            timeout=3.0,
        )
    )

    with (
        scoped(
            ScopeConfig(
                env_overlay={"PARENT_ONLY": "parent"},
                timeout=9.0,
            )
        ),
        manager as context,
    ):
        assert context.env_mode is EnvMode.REPLACE, (
            "the child must retain its explicit environment replacement mode"
        )
        assert dict(context.env_overlay or {}) == {"CHILD_ONLY": "child"}, (
            "replacement mode must omit the parent's environment overlay"
        )
        assert context.timeout == pytest.approx(3.0), (
            "an explicit child timeout must override the parent's timeout"
        )
