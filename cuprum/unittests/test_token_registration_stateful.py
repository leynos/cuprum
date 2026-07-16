"""Stateful tests for the canonical ``_TokenRegistration`` handle base.

All scope-registration handles (`AllowRegistration`, `HookRegistration`,
`EnvRegistration`) share one token-restoration implementation (#113). The
Hypothesis ``RuleBasedStateMachine`` below drives randomized sequences of
nested registrations, context-manager exits, and LIFO detaches across the
handle types and asserts the ``ContextVar`` is always restored to the exact
prior context — token discipline holds under nesting, context-manager exit,
and double-detach. Out-of-order detach (a documented hazard, not an error)
is pinned by a separate example test.
"""

from __future__ import annotations

import collections.abc as cabc
import contextvars
import typing as typ

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from cuprum import ECHO, LS
from cuprum.context import (
    AllowRegistration,
    CuprumContext,
    EnvRegistration,
    HookRegistration,
    ScopeConfig,
    after,
    allow,
    before,
    current_context,
    env,
    observe,
    scoped,
)

if typ.TYPE_CHECKING:
    from cuprum.context.registration import _TokenRegistration


def _noop_before(_cmd: object) -> None:
    """Before-hook that records nothing."""


def _noop_after(_cmd: object, _result: object) -> None:
    """After-hook that records nothing."""


def _noop_observe(_event: object) -> None:
    """Observe-hook that records nothing."""


type _Handle = AllowRegistration | HookRegistration | EnvRegistration
type _HandleFactory = cabc.Callable[[], _Handle]

_FACTORIES: tuple[tuple[str, _HandleFactory], ...] = (
    ("allow", lambda: allow(ECHO)),
    ("allow-two", lambda: allow(ECHO, LS)),
    ("before", lambda: before(_noop_before)),
    ("after", lambda: after(_noop_after)),
    ("observe", lambda: observe(_noop_observe)),
    ("env", lambda: env(CUPRUM_TEST_FLAG="1")),
    ("env-mapping", lambda: env({"CUPRUM_TEST_OTHER": "2"})),
)


class TokenRegistrationMachine(RuleBasedStateMachine):
    """Drive nested register/detach sequences across all handle types."""

    def __init__(self) -> None:
        """Record the baseline context and an empty handle stack."""
        super().__init__()
        self._baseline: CuprumContext = current_context()
        # Stack of (handle, prior context, installed context) tuples.
        self._stack: list[tuple[_TokenRegistration, CuprumContext, CuprumContext]] = []

    @rule(factory_entry=st.sampled_from(_FACTORIES))
    def register(self, factory_entry: tuple[str, _HandleFactory]) -> None:
        """Create a registration of the chosen kind, recording the prior context."""
        prior = current_context()
        _name, factory = factory_entry
        handle = factory()
        installed = current_context()
        self._stack.append((handle, prior, installed))
        assert installed is not prior, (
            "registering a handle must install a derived context"
        )

    @rule(factory_entry=st.sampled_from(_FACTORIES))
    def context_manager_restores_context(
        self, factory_entry: tuple[str, _HandleFactory]
    ) -> None:
        """Context-manager exit restores the context and detaches idempotently."""
        prior = current_context()
        _name, factory = factory_entry

        with factory() as handle:
            assert current_context() is not prior, (
                "registering a handle must install a derived context"
            )

        assert current_context() is prior, (
            "context-manager exit must restore the original context"
        )
        handle.detach()
        assert current_context() is prior, (
            "detaching after context-manager exit must leave the original "
            "context unchanged"
        )

    @precondition(lambda self: self._stack)
    @rule()
    def detach_innermost(self) -> None:
        """Detach the most recent registration; the prior context returns."""
        handle, prior, _installed = self._stack.pop()
        handle.detach()
        assert current_context() is prior, (
            "detach must restore the exact context captured at registration"
        )

    @precondition(lambda self: self._stack)
    @rule()
    def double_detach_is_idempotent(self) -> None:
        """Detaching the innermost handle twice changes nothing further."""
        handle, prior, _installed = self._stack.pop()
        handle.detach()
        after_first = current_context()
        handle.detach()
        assert current_context() is after_first, "second detach must be a no-op"
        assert after_first is prior, (
            "first detach must restore the captured prior context"
        )

    @rule(
        outer_factory_entry=st.sampled_from(_FACTORIES),
        inner_factory_entry=st.sampled_from(_FACTORIES),
    )
    def non_lifo_detach_restores_captured_snapshots(
        self,
        outer_factory_entry: tuple[str, _HandleFactory],
        inner_factory_entry: tuple[str, _HandleFactory],
    ) -> None:
        """Out-of-order detaches restore each handle's captured snapshot."""
        caller_context = current_context()
        with scoped(ScopeConfig()):
            outer_prior = current_context()
            _outer_name, outer_factory = outer_factory_entry
            outer = outer_factory()
            inner_prior = current_context()
            _inner_name, inner_factory = inner_factory_entry
            inner = inner_factory()

            outer.detach()
            assert current_context() is outer_prior, (
                "outer non-LIFO detach must restore its captured prior context"
            )

            inner.detach()
            assert current_context() is inner_prior, (
                "inner non-LIFO detach must restore its captured prior context"
            )

        assert current_context() is caller_context, (
            "scope exit must restore the caller context"
        )

    @invariant()
    def stack_depth_matches_context_nesting(self) -> None:
        """With an empty stack the baseline context is active again."""
        if not self._stack:
            assert current_context() is self._baseline, (
                "an empty stack must leave the baseline context active"
            )
        else:
            _handle, _prior, installed = self._stack[-1]
            assert current_context() is installed, (
                "a non-empty stack must leave its top context active"
            )

    def teardown(self) -> None:
        """Detach any remaining handles in LIFO order."""
        while self._stack:
            handle, _prior, _installed = self._stack.pop()
            handle.detach()
        assert current_context() is self._baseline, (
            "teardown must restore the exact baseline context"
        )


TestTokenRegistrationMachine = TokenRegistrationMachine.TestCase
TestTokenRegistrationMachine.settings = settings(
    max_examples=40,
    stateful_step_count=20,
    deadline=None,
)


class TestTokenRegistrationExamples:
    """Pinned example scenarios for token-restoration edge cases."""

    def test_unsupported_hook_type_is_rejected(self) -> None:
        """Invalid hook types must not be silently installed as observe hooks."""
        baseline = current_context()
        invalid_hook_type = typ.cast(
            "typ.Literal['before', 'after', 'observe']",
            "invalid",
        )

        with pytest.raises(ValueError, match="Unsupported hook type: invalid"):
            HookRegistration(_noop_observe, invalid_hook_type)

        assert current_context() is baseline, (
            "rejecting an unsupported hook type must not change the active context"
        )

    def test_out_of_order_detach_restores_outer_snapshot(self) -> None:
        """Example: non-LIFO detach restores the outer snapshot, as documented.

        Detaching an outer registration while an inner one is still attached
        resets the ``ContextVar`` to the outer handle's snapshot, discarding the
        inner overlay — the documented hazard that motivates preferring ``with``
        blocks. The experiment runs inside a ``scoped`` guard so the leaked
        state cannot pollute other tests: the late ``inner.detach()`` restores
        inner's own snapshot, which still carries the outer overlay, before the
        surrounding registrations are cleaned up in LIFO order.
        """
        with scoped(ScopeConfig()):
            baseline = current_context()
            allow_registration = allow(ECHO)
            hook_registration = before(_noop_before)
            pre_env = current_context()
            outer = env(CUPRUM_TEST_OUTER="outer")
            inner = env(CUPRUM_TEST_INNER="inner")

            outer.detach()

            restored = current_context()
            assert restored is pre_env, (
                "outer detach must restore the pre-outer snapshot, discarding inner"
            )
            assert restored.is_allowed(ECHO), (
                "outer detach must preserve ECHO access in the restored context"
            )
            assert _noop_before in restored.before_hooks, (
                "outer detach must preserve the before hook in the restored context"
            )
            overlay = restored.env_overlay or {}
            assert "CUPRUM_TEST_INNER" not in overlay, (
                "outer detach must drop the inner env overlay"
            )

            # The inner detach stays safe (idempotent token discipline) but
            # restores its own snapshot — the context with the outer overlay
            # attached. This is exactly the documented non-LIFO hazard.
            inner.detach()
            leaked_context = current_context()
            assert leaked_context.is_allowed(ECHO), (
                "inner detach must still preserve ECHO access"
            )
            assert _noop_before in leaked_context.before_hooks, (
                "inner detach must still preserve the before hook"
            )
            leaked = leaked_context.env_overlay or {}
            assert leaked.get("CUPRUM_TEST_OUTER") == "outer", (
                "inner detach must restore the outer env overlay"
            )

            hook_registration.detach()
            allow_registration.detach()
            assert current_context() is baseline, (
                "cleanup must restore the scoped baseline context"
            )

    def test_failed_cross_context_detach_can_be_retried(self) -> None:
        """A failed reset must not poison a registration handle.

        ``ContextVar`` tokens can only be reset inside the context that created
        them. A cross-context detach raises ``ValueError``; the original context
        must still be able to detach the handle afterwards.
        """
        with scoped(ScopeConfig()):
            baseline = current_context()
            handle = env(CUPRUM_TEST_RETRY="retry")

            with pytest.raises(ValueError, match="different Context"):
                contextvars.Context().run(handle.detach)

            assert current_context() is not baseline, (
                "failed cross-context detach must leave the handle active in "
                "the original context"
            )
            handle.detach()
            assert current_context() is baseline, (
                "retrying detach in the original context must restore the "
                "baseline context"
            )
