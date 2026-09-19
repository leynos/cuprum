"""Structured-observation contracts for native-pump cancellation cleanup."""

from __future__ import annotations

import logging
import typing as typ

from cuprum import (
    _pipeline_stream_cleanup_observation,
    _pipeline_stream_fds,
)
from cuprum._pipeline_native_pump_types import _RustPumpState
from cuprum.pump_observation import observe_pump

if typ.TYPE_CHECKING:
    import pytest

    from cuprum.pump_events import PumpEvent

_LOGGER = logging.getLogger("cuprum._pipeline_streams")


class _CompletedCleanupState:
    """Model a callback that atomically completed cleanup before grace expiry."""

    def defer_cleanup(self) -> bool:
        """Report that worker completion already owns the terminal transition."""
        return False


def test_cleanup_observations_keep_phase_specific_debug_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each cleanup phase adds only its documented bounded DEBUG fields."""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER.name):
        _pipeline_stream_cleanup_observation._log_native_pump_cleanup(
            _LOGGER,
            "cleanup_started",
        )
        _pipeline_stream_cleanup_observation._log_native_pump_cleanup(
            _LOGGER,
            "cleanup_completed",
            0.25,
        )
        _pipeline_stream_cleanup_observation._log_native_pump_cleanup(
            _LOGGER,
            "cleanup_grace_expired",
            0.5,
        )
        _pipeline_stream_cleanup_observation._log_native_pump_cleanup(
            _LOGGER,
            "cleanup_deferred",
        )

    records = [
        record.__dict__
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "rust_pump_cleanup"
    ]
    assert [
        {key: value for key, value in record.items() if key.startswith("cuprum_")}
        for record in records
    ] == [
        {
            "cuprum_action": "rust_pump_cleanup",
            "cuprum_operation": "native_pump_cleanup",
            "cuprum_outcome": "started",
        },
        {
            "cuprum_action": "rust_pump_cleanup",
            "cuprum_operation": "native_pump_cleanup",
            "cuprum_outcome": "completed",
            "cuprum_duration_s": 0.25,
        },
        {
            "cuprum_action": "rust_pump_cleanup",
            "cuprum_operation": "native_pump_cleanup",
            "cuprum_outcome": "grace_expired",
            "cuprum_elapsed_s": 0.5,
        },
        {
            "cuprum_action": "rust_pump_cleanup",
            "cuprum_operation": "native_pump_cleanup",
            "cuprum_outcome": "deferred",
        },
    ], f"cleanup DEBUG records must retain their exact bounded fields: {records}"


def test_completed_worker_wins_over_late_grace_expiry() -> None:
    """A losing grace transition must not report a deferred cleanup lifecycle."""
    events: list[PumpEvent] = []
    wait = _pipeline_stream_cleanup_observation._NativePumpCleanupWait(
        _LOGGER,
        monotonic_clock=lambda: 1.0,
        cleanup_grace_s=0.5,
        state=_CompletedCleanupState(),
    )

    with observe_pump(events.append):
        assert not _pipeline_stream_cleanup_observation._defer_native_pump_cleanup(
            wait=wait,
            started_at=0.0,
        )

    assert events == [], (
        "worker completion must suppress a grace-expiry event after it wins"
    )


def test_only_a_winning_deferral_releases_the_paused_reader() -> None:
    """The reader release belongs to the deferral decision, not to the wait.

    A deferred hop outlives the loop that paused its reader, so the release is
    what keeps that descriptor from surviving past ``loop.close()``. It must
    fire on every call that wins the deferral — the transport's own ``close``
    is idempotent, so a repeat is harmless — and must not fire once worker
    completion owns the transition, because that hand-off still belongs to the
    loop and would otherwise lose a reader nothing has finished reading.
    """
    releases: list[None] = []
    state = _RustPumpState(
        reader_fd=-1,
        writer_fd=-1,
        blocking_mode_guard=typ.cast(
            "_pipeline_stream_fds._BlockingModeGuard", object()
        ),
        resume_reader=None,
        release_reader=lambda: releases.append(None),
    )

    assert state.defer_cleanup() is True, "the first deferral must win"
    assert releases == [None], "a winning deferral must release the paused reader"

    completed = _RustPumpState(
        reader_fd=-1,
        writer_fd=-1,
        blocking_mode_guard=typ.cast(
            "_pipeline_stream_fds._BlockingModeGuard", object()
        ),
        resume_reader=None,
        release_reader=lambda: releases.append(None),
    )
    assert completed.complete_cleanup() is False, (
        "no deferral has won yet, so completion must not report one"
    )
    assert completed.defer_cleanup() is False, (
        "worker completion must win the deferral decision once it has completed"
    )
    assert releases == [None], (
        "a deferral that loses to worker completion must not release the reader"
    )
