"""Shim-level hand-off outcomes for the optional Rust stream extension."""

from __future__ import annotations

import typing as typ
from unittest import mock

import pytest

from cuprum import _streams_rs
from cuprum.pump_events import PumpEvent, RustPumpHandoffOutcome
from cuprum.pump_observation import observe_pump


class _NativeLoadError(ImportError):
    """Signal a native extension that cannot be loaded."""

    def __init__(self) -> None:
        """Initialize the test double's stable diagnostic message."""
        super().__init__("native extension unavailable")


class _WindowsTransferError(OSError):
    """Signal a failed Windows writer-handle transfer."""

    def __init__(self) -> None:
        """Initialize the test double's stable diagnostic message."""
        super().__init__("DuplicateHandle failed")


class _MetricsObserverError(RuntimeError):
    """Signal a metrics collector that fails while recording an outcome."""

    def __init__(self) -> None:
        """Initialize the test double's stable diagnostic message."""
        super().__init__("metrics unavailable")


def _outcomes(events: list[PumpEvent]) -> list[RustPumpHandoffOutcome]:
    """Return hand-off outcomes from the recorded mixed pump event stream."""
    return [
        typ.cast("RustPumpHandoffOutcome", event.outcome)
        for event in events
        if event.phase == "handoff"
    ]


def _install_native_pump(
    monkeypatch: pytest.MonkeyPatch,
    native_pump: mock.Mock,
) -> None:
    """Install a minimal native module whose pump can be controlled by a test."""
    native_module = mock.Mock()
    native_module.rust_pump_stream = native_pump
    monkeypatch.setattr(_streams_rs, "_load_native", lambda: native_module)
    monkeypatch.setattr(_streams_rs, "_convert_fd_for_platform", lambda fd: fd)


def _install_observed_native_pump(
    monkeypatch: pytest.MonkeyPatch,
    native_pump: mock.Mock,
) -> tuple[list[PumpEvent], mock.Mock]:
    """Install a controlled native pump and capture pre-native cleanup."""
    events: list[PumpEvent] = []
    close_writer = mock.Mock()
    _install_native_pump(monkeypatch, native_pump)
    monkeypatch.setattr(
        _streams_rs,
        "_close_writer_after_pre_native_failure",
        close_writer,
    )
    return events, close_writer


def _assert_handoff_outcome(
    events: list[PumpEvent],
    expected: RustPumpHandoffOutcome,
    message: str,
) -> None:
    """Assert that one call emitted the expected bounded hand-off outcome."""
    assert _outcomes(events) == [expected], message


def test_native_load_failure_emits_one_bounded_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing extension remains a worker-side failure after submission."""
    events: list[PumpEvent] = []
    close_writer = mock.Mock()

    def fail_load() -> typ.NoReturn:
        """Model a native import failure before the callable exists."""
        raise _NativeLoadError

    monkeypatch.setattr(_streams_rs, "_load_native", fail_load)
    monkeypatch.setattr(
        _streams_rs,
        "_close_writer_after_pre_native_failure",
        close_writer,
    )
    with observe_pump(events.append), pytest.raises(ImportError, match="unavailable"):
        _streams_rs.rust_pump_stream(11, 12)

    close_writer.assert_called_once_with(12)
    assert _outcomes(events) == [RustPumpHandoffOutcome.NATIVE_LOAD_FAILED], (
        "native-load failure must emit exactly its closed outcome"
    )


def test_invalid_buffer_size_emits_one_bounded_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation failure is recorded before the writer reaches Rust."""
    native_pump = mock.Mock(return_value=0)
    events, close_writer = _install_observed_native_pump(monkeypatch, native_pump)
    with observe_pump(events.append), pytest.raises(ValueError, match="greater"):
        _streams_rs.rust_pump_stream(11, 12, buffer_size=0)

    close_writer.assert_called_once_with(12)
    native_pump.assert_not_called()
    _assert_handoff_outcome(
        events,
        RustPumpHandoffOutcome.BUFFER_VALIDATION_FAILED,
        "invalid buffer size must emit exactly its closed outcome",
    )


def test_windows_writer_transfer_failure_emits_one_bounded_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Windows transfer closes its worker-side writer once."""
    events: list[PumpEvent] = []
    close_writer = mock.Mock()
    native_pump = mock.Mock(return_value=0)
    _install_native_pump(monkeypatch, native_pump)
    monkeypatch.setattr(
        _streams_rs,
        "_close_writer_after_pre_native_failure",
        close_writer,
    )

    def fail_transfer(writer_fd: int) -> typ.NoReturn:
        """Model failure while duplicating a Windows writer handle."""
        del writer_fd
        raise _WindowsTransferError

    monkeypatch.setattr(_streams_rs, "_transfer_writer_fd_for_platform", fail_transfer)
    with observe_pump(events.append), pytest.raises(OSError, match="DuplicateHandle"):
        _streams_rs.rust_pump_stream(11, 12)

    close_writer.assert_called_once_with(12)
    native_pump.assert_not_called()
    assert _outcomes(events) == [
        RustPumpHandoffOutcome.PLATFORM_WRITER_TRANSFER_FAILED
    ], "Windows transfer failure must emit exactly its closed outcome"


def test_native_io_failure_emits_one_bounded_outcome_without_python_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An I/O failure leaves closure of the transferred writer to Rust."""
    native_pump = mock.Mock(side_effect=OSError("broken pipe"))
    events, close_writer = _install_observed_native_pump(monkeypatch, native_pump)
    with observe_pump(events.append), pytest.raises(OSError, match="broken pipe"):
        _streams_rs.rust_pump_stream(11, 12)

    close_writer.assert_not_called()
    _assert_handoff_outcome(
        events,
        RustPumpHandoffOutcome.NATIVE_IO_FAILED,
        "native I/O failure must emit exactly its closed outcome",
    )


def test_failing_observer_cannot_replace_a_native_io_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observer failure must not displace the shim's original I/O exception."""
    native_pump = mock.Mock(side_effect=OSError("broken pipe"))
    _install_native_pump(monkeypatch, native_pump)

    def fail_observer(event: PumpEvent) -> typ.NoReturn:
        """Model a metrics collector that raises while recording an outcome."""
        del event
        raise _MetricsObserverError

    with observe_pump(fail_observer), pytest.raises(OSError, match="broken pipe"):
        _streams_rs.rust_pump_stream(11, 12)
