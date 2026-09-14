"""Structured-observation contracts for native-pump cancellation cleanup."""

from __future__ import annotations

import logging
import typing as typ

from cuprum import _pipeline_stream_cleanup_observation
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
