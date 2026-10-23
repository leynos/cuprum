"""Stateful contracts for reusable scoped context managers.

The state machine below generates nested entries, LIFO exits, manager reuse,
and rejected foreign-manager exits. It checks policy intersection after every
entry and exact context restoration after every successful exit.
"""

from __future__ import annotations

from contextlib import ExitStack

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from cuprum import ECHO, LS, Program
from cuprum.context import CuprumContext, ScopeConfig, current_context, scoped

_SCOPE_CONFIGS: dict[str, ScopeConfig] = {
    "unrestricted": ScopeConfig(),
    "echo": ScopeConfig(allowlist=frozenset([ECHO])),
    "ls": ScopeConfig(allowlist=frozenset([LS])),
    "both": ScopeConfig(allowlist=frozenset([ECHO, LS])),
    "empty": ScopeConfig(allowlist=frozenset()),
}
_SCOPE_NAMES = tuple(_SCOPE_CONFIGS)


def _expected_allowlist(
    parent: CuprumContext,
    requested: frozenset[Program] | None,
) -> frozenset[Program]:
    """Return the policy intersection for a single generated entry."""
    if requested is None:
        return parent.allowlist
    if parent._allowlist_is_restricted and not parent.allowlist:
        return parent.allowlist
    if not parent.allowlist:
        return requested
    return parent.allowlist & requested


class ScopedEntryMachine(RuleBasedStateMachine):
    """Exercise context derivation and restoration across generated entries."""

    def __init__(self) -> None:
        """Start from the current context and prepare reusable managers."""
        super().__init__()
        self._baseline = current_context()
        self._managers = {
            name: scoped(config) for name, config in _SCOPE_CONFIGS.items()
        }
        self._stack: list[tuple[str, CuprumContext, CuprumContext, ExitStack]] = []

    @precondition(lambda self: len(self._stack) < 6)
    @rule(name=st.sampled_from(_SCOPE_NAMES))
    def enter_manager(self, name: str) -> None:
        """Enter a reusable manager and verify its allowlist intersection."""
        parent = current_context()
        entry = ExitStack()
        entered = entry.enter_context(self._managers[name])
        self._stack.append((name, parent, entered, entry))
        requested = _SCOPE_CONFIGS[name].allowlist
        assert entered.allowlist == _expected_allowlist(parent, requested)
        assert current_context() is entered

    @precondition(lambda self: bool(self._stack))
    @rule()
    def exit_top_manager(self) -> None:
        """Exit the top entry and restore its exact parent context."""
        _name, parent, _entered, entry = self._stack.pop()
        entry.close()
        assert current_context() is parent

    @precondition(lambda self: bool(self._stack))
    @rule(data=st.data())
    def reject_foreign_manager_exit(self, data: st.DataObject) -> None:
        """Reject a non-top manager without changing the active context."""
        active_name = self._stack[-1][0]
        foreign_names = tuple(name for name in _SCOPE_NAMES if name != active_name)
        foreign_name = data.draw(st.sampled_from(foreign_names))
        active_context = current_context()

        with pytest.raises(RuntimeError, match="LIFO order"):
            self._managers[foreign_name].__exit__(None, None, None)

        assert current_context() is active_context

    @invariant()
    def active_context_matches_stack(self) -> None:
        """Keep the context bound to the latest successful entry."""
        expected = self._baseline if not self._stack else self._stack[-1][2]
        assert current_context() is expected

    def teardown(self) -> None:
        """Unwind entries left by the final generated action."""
        while self._stack:
            _name, parent, _entered, entry = self._stack.pop()
            entry.close()
            assert current_context() is parent
        assert current_context() is self._baseline


def test_scoped_entry_state_machine() -> None:
    """Generate bounded nested, reused, rejected and restored scope entries."""
    run_state_machine_as_test(
        ScopedEntryMachine,
        settings=settings(max_examples=40, stateful_step_count=30, deadline=None),
    )
