"""Stateful contracts for identity-based hook removal.

:class:`~cuprum._scope_registration._IdentityTupleRegistration` removes a
handle's own hook from a context-local tuple *by identity* rather than by
equality, scanning from the end so the newest registration is the one dropped.
Both halves matter and neither is visible to a test that registers only
distinct bound methods: an equality match would detach a hook its owner still
holds, and a first-match scan would drop the older of two identical
registrations.

The :class:`~hypothesis.stateful.RuleBasedStateMachine` below drives generated
sequences of registration, detach and context-manager exit over hooks that are
deliberately equal-but-distinct, and asserts that the context-local tuple always
matches the surviving-handle model *by identity*. Comparing identity rather than
equality is what keeps the model itself from going blind: two hooks built with
the same tag compare equal, so an equality check could not say which of them
survived.
"""

from __future__ import annotations

import dataclasses as dc
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

from cuprum.stream_observation import (
    StreamOperationHookRegistration,
    current_stream_operation_hooks,
    observe_stream_operation,
)

if typ.TYPE_CHECKING:
    from cuprum.stream_events import StreamOperationEvent


@dc.dataclass(frozen=True, slots=True)
class _RecordingHook:
    """A stream-operation hook whose instances compare equal when tagged alike.

    ``dataclass(eq=True)`` is what makes the identity discipline observable: two
    hooks built with the same ``tag`` are equal but distinct objects, so a
    removal that matched on equality would take the wrong one. The class holds
    no other state on purpose — nothing here emits an event, so the tests assert
    on which hook object survived rather than on what it received.
    """

    tag: int

    def __call__(self, event: StreamOperationEvent) -> None:
        """Accept ``event`` without recording it; identity is what is asserted."""


class IdentityRegistrationMachine(RuleBasedStateMachine):
    """Drive register/detach/exit sequences over equal-but-distinct hooks."""

    def __init__(self) -> None:
        """Start from an empty tuple with an empty detached-handle record."""
        super().__init__()
        self._baseline = current_stream_operation_hooks()
        self._hooks = self._baseline
        # Stack of (handle, hook) in registration order.
        self._stack: list[tuple[StreamOperationHookRegistration, _RecordingHook]] = []
        self._tag = 0

    def _actual(self) -> tuple[object, ...]:
        """Return the live context-local hook tuple."""
        return current_stream_operation_hooks()

    def _assert_model(self) -> None:
        """Assert the live tuple is exactly the registered hooks, in order.

        Compared element-wise by identity rather than by equality: the fixture
        hooks are equal by tag, so an equality comparison would accept a
        surviving hook that belongs to a detached handle.
        """
        expected = tuple(hook for _handle, hook in self._stack)
        actual = self._actual()
        assert len(actual) == len(expected), (
            f"the registered tuple must hold {len(expected)} hooks, got {actual}"
        )
        for index, (live, want) in enumerate(zip(actual, expected, strict=True)):
            assert live is want, (
                f"hook at index {index} must be the surviving handle's own "
                f"object, got {live!r} where {want!r} was expected"
            )

    @rule(tag=st.integers(min_value=0, max_value=3))
    def register(self, tag: int) -> None:
        """Register a hook, optionally equal to an existing one."""
        hook = _RecordingHook(tag)
        handle = observe_stream_operation(hook)
        self._stack.append((handle, hook))
        self._assert_model()

    @precondition(lambda self: len(self._stack) >= 2)
    @rule(data=st.data())
    def detach_any(self, data: st.DataObject) -> None:
        """Detach a chosen live handle, in or out of last-in-first-out order."""
        index = data.draw(st.integers(min_value=0, max_value=len(self._stack) - 1))
        handle, _hook = self._stack.pop(index)
        handle.detach()
        self._assert_model()

    @precondition(lambda self: bool(self._stack))
    @rule(data=st.data())
    def detach_repeatedly(self, data: st.DataObject) -> None:
        """Detaching one chosen handle twice must be harmless."""
        index = data.draw(st.integers(min_value=0, max_value=len(self._stack) - 1))
        handle, _hook = self._stack.pop(index)
        handle.detach()
        handle.detach()
        self._assert_model()

    @precondition(lambda self: bool(self._stack))
    @rule(data=st.data())
    def context_manager_exit(self, data: st.DataObject) -> None:
        """Entering the block is a no-op; leaving it detaches exactly this handle."""
        index = data.draw(st.integers(min_value=0, max_value=len(self._stack) - 1))
        handle, _hook = self._stack[index]
        before = self._actual()
        with handle as entered:
            assert entered is handle, "entry must yield the handle itself"
            assert self._actual() == before, (
                "the effect is installed at construction, so entry is a no-op"
            )
        self._stack.pop(index)
        self._assert_model()

    @invariant()
    def registered_tuple_matches_the_model(self) -> None:
        """Check the context-local tuple after every generated operation."""
        self._assert_model()

    def teardown(self) -> None:
        """Detach every remaining handle and restore the empty baseline."""
        while self._stack:
            handle, _hook = self._stack.pop()
            handle.detach()
        self._assert_model()
        assert self._actual() == self._baseline, (
            "state-machine cleanup must restore the exact baseline tuple"
        )


class _RaisingRelease(StreamOperationHookRegistration):
    """A handle whose release raises, so the retryable contract is reachable.

    ``_without_identity`` is total for a tuple target and cannot fail, so a
    stock handle cannot exercise the "failed release stays retryable" clause.
    Overriding ``_release`` is the smallest way to reach it through the shared
    ``detach`` that owns the contract.
    """

    def __init__(self, hook: _RecordingHook) -> None:
        """Register ``hook`` and arm the release to fail on its first call."""
        super().__init__(hook)
        self.attempts = 0

    def _release(self) -> None:
        """Fail on the first call, then delegate to the real release."""
        self.attempts += 1
        if self.attempts == 1:
            msg = "release failed on purpose"
            raise RuntimeError(msg)
        super()._release()


def test_failed_release_keeps_the_handle_retryable() -> None:
    """A release that raises must leave the handle detached-able again.

    ``detach`` marks the handle detached only after ``_release`` returns, so a
    raising release leaves the registration installed and the handle live. That
    ordering is what makes a retry possible at all, and it is shared by every
    handle, so it is pinned here on the identity path.
    """
    hook = _RecordingHook(tag=11)
    handle = _RaisingRelease(hook)

    with pytest.raises(RuntimeError, match="release failed on purpose"):
        handle.detach()
    assert handle.attempts == 1, "the failing release must have been attempted"
    assert current_stream_operation_hooks() == (hook,), (
        "a failed release must leave the registration installed"
    )

    handle.detach()
    assert handle.attempts == 2, "the retry must reach the release again"
    assert current_stream_operation_hooks() == (), (
        "the retry must remove the registration the failed release left behind"
    )


def test_identity_registration_machine() -> None:
    """Run the state machine with a bounded example budget."""
    run_state_machine_as_test(
        IdentityRegistrationMachine,
        settings=settings(max_examples=40, stateful_step_count=20, deadline=None),
    )


def test_equal_but_distinct_hooks_are_removed_by_identity() -> None:
    """Detaching one of two equal hooks must leave the other registered.

    This is the discriminating case the channel tests do not reach: they
    register distinct bound methods, so an equality match and an identity match
    agree on their outcome. Each handle here must remove its *own* hook even
    though both hooks compare equal.
    """
    first = _RecordingHook(tag=7)
    second = _RecordingHook(tag=7)
    assert first == second, "the fixture must supply equal-but-distinct hooks"
    assert first is not second, "the fixture must supply distinct objects"
    baseline = current_stream_operation_hooks()

    outer = observe_stream_operation(first)
    inner = observe_stream_operation(second)
    assert current_stream_operation_hooks() == (first, second), (
        "both equal-but-distinct hooks must be registered, in order"
    )

    inner.detach()
    assert current_stream_operation_hooks() == (first,), (
        "detaching the inner handle must leave the outer handle's equal hook"
    )
    assert current_stream_operation_hooks()[0] is first, (
        "the surviving hook must be the outer handle's own object"
    )

    outer.detach()
    assert current_stream_operation_hooks() == baseline, (
        "detaching the outer handle must restore the baseline tuple"
    )


def test_duplicate_registration_leaves_one_entry_per_handle() -> None:
    """Registering one object twice must take both registrations to clear.

    Each detach removes one entry and neither removes both, so detach count
    matches registration count.
    """
    hook = _RecordingHook(tag=1)
    baseline = current_stream_operation_hooks()

    outer = observe_stream_operation(hook)
    inner = observe_stream_operation(hook)
    assert current_stream_operation_hooks() == (hook, hook), (
        "registering one object twice must install two entries"
    )

    inner.detach()
    assert current_stream_operation_hooks() == (hook,), (
        "one registration must remain after the newest is detached"
    )
    outer.detach()
    assert current_stream_operation_hooks() == baseline, (
        "both registrations must be removed to clear the duplicate"
    )


def test_end_first_scan_drops_the_newest_of_two_equal_entries() -> None:
    """An intervening distinct hook makes the scan direction observable.

    Two entries for the same object are indistinguishable on their own, so the
    direction only shows when a *different* hook sits between them. Detaching
    the older of the two equal registrations must drop the later entry and
    leave the pair ordered ``(own, interleaved)``; a first-match scan would
    drop the earlier entry instead and leave ``(interleaved, own)``.
    """
    own = _RecordingHook(tag=5)
    interleaved = _RecordingHook(tag=6)
    baseline = current_stream_operation_hooks()

    outer = observe_stream_operation(own)
    middle = observe_stream_operation(interleaved)
    inner = observe_stream_operation(own)

    outer.detach()
    live = current_stream_operation_hooks()
    assert len(live) == 2, f"exactly two registrations must remain, got {live!r}"
    assert live[0] is own, (
        "the older equal entry must survive, so the newest was dropped"
    )
    assert live[1] is interleaved, "the interleaved hook must keep its position"

    middle.detach()
    inner.detach()
    assert current_stream_operation_hooks() == baseline, (
        "the interleaved and inner registrations must restore the baseline"
    )


def test_absent_hook_detach_is_harmless() -> None:
    """Detaching a hook that was already removed must leave the tuple unchanged."""
    hook = _RecordingHook(tag=2)
    other = _RecordingHook(tag=3)
    registration = observe_stream_operation(hook)
    registration.detach()
    survivor = observe_stream_operation(other)

    registration.detach()
    assert current_stream_operation_hooks() == (other,), (
        "a repeated detach must not remove a hook the handle never installed"
    )
    survivor.detach()
    assert current_stream_operation_hooks() == (), (
        "the surviving handle must have been the only remaining registration"
    )
