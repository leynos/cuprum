"""Stateful contract checks for native-pump cancellation cleanup."""

from __future__ import annotations

import dataclasses
import typing as typ

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

type _CleanupPhase = typ.Literal["cleanup_started", "cleanup_completed"]


@dataclasses.dataclass(frozen=True)
class _CleanupTelemetry:
    """One modelled cleanup telemetry record."""

    phase: _CleanupPhase
    duration_s: float | None = None


class _NativePumpCleanupMachine(RuleBasedStateMachine):
    """Model cancellation cleanup independently of the executor implementation."""

    def __init__(self) -> None:
        """Start with a native worker that owns its descriptors."""
        super().__init__()
        self._worker_owns_descriptors = True
        self._cancelled = False
        self._worker_completed = False
        self._restore_attempted = False
        self._restore_failures = 0
        self._cleanup_future_settlements = 0
        self._telemetry: list[_CleanupTelemetry] = []
        self._restored_while_owned = False
        self._closed_while_owned = False

    @precondition(lambda self: not self._cancelled and self._worker_owns_descriptors)
    @rule()
    def first_cancellation(self) -> None:
        """Begin one cancellation-cleanup lifecycle."""
        self._cancelled = True
        self._telemetry.append(_CleanupTelemetry("cleanup_started"))

    @precondition(lambda self: self._cancelled)
    @rule()
    def repeated_cancellation(self) -> None:
        """Model another cancellation request while cleanup is already pending."""

    @precondition(lambda self: self._worker_owns_descriptors)
    @rule()
    def worker_completion(self) -> None:
        """Release native worker descriptor ownership."""
        self._worker_owns_descriptors = False
        self._worker_completed = True

    @precondition(lambda self: not self._cancelled and self._worker_completed)
    @rule()
    def restart_worker(self) -> None:
        """Start a new worker after one completes without cancellation."""
        self._worker_owns_descriptors = True
        self._worker_completed = False

    @precondition(
        lambda self: (
            self._cancelled and self._worker_completed and not self._restore_attempted
        )
    )
    @rule()
    def restore_succeeds(self) -> None:
        """Restore descriptors and complete cancellation cleanup successfully."""
        self._complete_cleanup()

    @precondition(
        lambda self: (
            self._cancelled and self._worker_completed and not self._restore_attempted
        )
    )
    @rule()
    def restore_fails(self) -> None:
        """Record a restore failure while still completing cancellation cleanup."""
        self._restore_failures += 1
        self._complete_cleanup()

    def _complete_cleanup(self) -> None:
        """Model the one terminal cleanup transition after worker completion."""
        self._restore_attempted = True
        self._restored_while_owned |= self._worker_owns_descriptors
        self._closed_while_owned |= self._worker_owns_descriptors
        self._cleanup_future_settlements += 1
        self._telemetry.append(_CleanupTelemetry("cleanup_completed", 1.0))

    @invariant()
    def completion_settles_once_and_after_the_worker(self) -> None:
        """Only a worker-free lifecycle can settle its cleanup future once."""
        assert self._cleanup_future_settlements <= 1, (
            "cleanup completion future must be settled at most once"
        )
        if self._cleanup_future_settlements:
            assert self._worker_completed, (
                "cleanup completion must wait for worker completion"
            )
            assert self._restore_attempted, (
                "cleanup completion must follow descriptor restoration"
            )

    @invariant()
    def cleanup_telemetry_is_ordered_and_completion_only_for_duration(self) -> None:
        """Cleanup emits at most one ordered start/completion pair."""
        phases = [event.phase for event in self._telemetry]
        assert phases.count("cleanup_started") <= 1, (
            "repeated cancellation must not duplicate cleanup-start telemetry"
        )
        assert phases.count("cleanup_completed") <= 1, (
            "repeated cancellation must not duplicate cleanup-complete telemetry"
        )
        if "cleanup_completed" in phases:
            assert phases == ["cleanup_started", "cleanup_completed"], (
                "cleanup completion must follow cleanup start"
            )
        assert all(
            (event.phase == "cleanup_completed") == (event.duration_s is not None)
            for event in self._telemetry
        ), "only cleanup completion may carry a duration"

    @invariant()
    def restore_failures_cannot_strand_or_violate_ownership(self) -> None:
        """Restore failure is recorded without retaining worker-owned descriptors."""
        assert not self._restored_while_owned, (
            "descriptor restoration must not occur while the worker owns descriptors"
        )
        assert not self._closed_while_owned, (
            "descriptor closure must not occur while the worker owns descriptors"
        )
        if self._restore_failures:
            assert self._cleanup_future_settlements == 1, (
                "a restore failure must still settle cancellation cleanup"
            )


TestNativePumpCleanupLifecycle = _NativePumpCleanupMachine.TestCase
TestNativePumpCleanupLifecycle.settings = settings(
    max_examples=60,
    stateful_step_count=12,
    deadline=None,
)
