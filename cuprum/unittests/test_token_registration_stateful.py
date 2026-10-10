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
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from cuprum import ECHO, LS, Program
from cuprum.context import (
    AllowRegistration,
    CuprumContext,
    EnvRegistration,
    ExecutableBindingRegistration,
    HookRegistration,
    ScopeConfig,
    after,
    allow,
    before,
    bind_executable,
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


type _Handle = (
    AllowRegistration
    | HookRegistration
    | EnvRegistration
    | ExecutableBindingRegistration
)
type _HandleFactory = cabc.Callable[[], _Handle]

# Programs recorded whenever a generated sequence installs a binding. The
# alphabet is sampled at random, so a run can legitimately never reach a
# binding factory; `test_binding_factories_are_sampled` asserts this list is
# populated rather than trusting that the alphabet was reached. Recording is
# driven by the handle type rather than by the factory entries, so a binding
# factory added later is covered without editing this list.
_bound_programs: list[Program] = []


def _record_binding(handle: _TokenRegistration) -> None:
    """Record a binding handle, so the non-vacuity check can observe sampling."""
    if isinstance(handle, ExecutableBindingRegistration):
        _bound_programs.append(handle.program)


def _bind_echo() -> ExecutableBindingRegistration:
    """Bind ``ECHO`` to an absolute executable."""
    return bind_executable(ECHO, "/opt/tools/echo")


def _bind_echo_nested() -> ExecutableBindingRegistration:
    """Bind ``ECHO`` to a relative executable, resolved against the cwd."""
    return bind_executable(ECHO, "tools/echo", allow_relative=True)


def _bind_ls() -> ExecutableBindingRegistration:
    """Bind ``LS``, so a sequence sampling both exercises the layer merge."""
    return bind_executable(LS, "/opt/tools/ls")


_FACTORIES: tuple[tuple[str, _HandleFactory], ...] = (
    ("allow", lambda: allow(ECHO)),
    ("allow-two", lambda: allow(ECHO, LS)),
    ("before", lambda: before(_noop_before)),
    ("after", lambda: after(_noop_after)),
    ("observe", lambda: observe(_noop_observe)),
    ("env", lambda: env(CUPRUM_TEST_FLAG="1")),
    ("env-mapping", lambda: env({"CUPRUM_TEST_OTHER": "2"})),
    # Two bindings for distinct programs, so a generated sequence that samples
    # the pair exercises the layer merge rather than only a key replacement.
    ("bind", _bind_echo),
    ("bind-nested", _bind_echo_nested),
    ("bind-two", _bind_ls),
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
        _record_binding(handle)
        installed = current_context()
        assert installed is not prior, (
            "registering a handle must install a derived context"
        )
        self._stack.append((handle, prior, installed))

    @rule(factory_entry=st.sampled_from(_FACTORIES))
    def context_manager_restores_context(
        self, factory_entry: tuple[str, _HandleFactory]
    ) -> None:
        """Context-manager exit restores the context and detaches idempotently."""
        prior = current_context()
        _name, factory = factory_entry

        with factory() as handle:
            _record_binding(handle)
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
    def active_context_matches_stack_top(self) -> None:
        """Ensure the active context matches the latest attached registration."""
        expected = self._baseline if not self._stack else self._stack[-1][2]
        assert current_context() is expected, (
            "the active ContextVar value must equal the latest attached "
            "registration's context"
        )

    def teardown(self) -> None:
        """Detach any remaining handles in LIFO order."""
        while self._stack:
            handle, _prior, _installed = self._stack.pop()
            handle.detach()
        assert current_context() is self._baseline, (
            "teardown must restore the exact baseline context"
        )


def test_binding_factories_are_sampled() -> None:
    """The generated sequences must actually reach a binding factory.

    This is the non-vacuity guard for the binding entries in ``_FACTORIES``.
    The alphabet is sampled at random, so a run that never reached
    ``bind``/``bind-nested``/``bind-two`` would satisfy every invariant in the
    machine while proving nothing about bindings. The machine is therefore run
    separately and the run is asserted to have recorded at least one installed
    binding; ``_bound_programs`` is cleared first so the result describes this
    run and not a previous one.

    What this guard establishes is deliberately narrow, and the budget is not
    part of the argument: a pinned ``max_examples``/``stateful_step_count``
    bounds how much is generated, it does not make any particular sequence
    likely. The guard proves that *at least one* binding factory ran. It does
    not prove that all three ran, that an absolute and a relative binding
    overlapped, or that same-key override and distinct-key merge both occurred;
    those are sampled rather than guaranteed. Binding-*content* correctness is
    not this machine's subject at all — its invariant is object identity of the
    restored context — and is carried by the named tests in
    ``test_context_isolation.py``, which check the programs before, during, and
    after an override.

    The recorded programs are checked to be a subset of the bound ones, which
    fails if ``_record_binding`` ever records a program no entry binds.
    """
    _bound_programs.clear()
    run_state_machine_as_test(
        TokenRegistrationMachine,
        settings=settings(
            max_examples=15,
            stateful_step_count=10,
            deadline=None,
            database=None,
        ),
    )
    assert _bound_programs, (
        "no generated sequence installed an executable binding, so the "
        "binding entries of _FACTORIES went unexercised and this suite proves "
        "nothing about them"
    )
    assert set(_bound_programs) <= {ECHO, LS}, (
        f"a binding factory recorded an unexpected program, got {_bound_programs!r}"
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

    def test_nested_env_detach_restores_outer_overlay(self) -> None:
        """LIFO env detach restores the outer registration's overlay."""
        with scoped(ScopeConfig()):
            baseline = current_context()
            outer = env(CUPRUM_TEST_OUTER="outer")
            try:
                outer_context = current_context()
                inner = env(CUPRUM_TEST_INNER="inner")
                try:
                    overlay = current_context().env_overlay or {}
                    assert overlay.get("CUPRUM_TEST_OUTER") == "outer", (
                        "nested overlays must retain the outer key"
                    )
                    assert overlay.get("CUPRUM_TEST_INNER") == "inner", (
                        "nested overlays must include the inner key"
                    )

                    inner.detach()

                    assert current_context() is outer_context, (
                        "inner detach must restore the outer context"
                    )
                    restored_overlay = current_context().env_overlay or {}
                    assert restored_overlay.get("CUPRUM_TEST_OUTER") == "outer", (
                        "inner detach must retain the outer overlay"
                    )
                    assert "CUPRUM_TEST_INNER" not in restored_overlay, (
                        "inner detach must discard the inner overlay"
                    )
                finally:
                    inner.detach()
            finally:
                outer.detach()

            assert current_context() is baseline, (
                "outer detach must restore the scoped baseline context"
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
