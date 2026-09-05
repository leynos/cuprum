"""Shim-level hand-off outcomes for the optional Rust stream extension."""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ
from unittest import mock

import pytest

from cuprum import _streams_rs
from cuprum.adapters.pump_metrics import RUST_PUMP_HANDOFF_TOTAL, PumpMetricsHook
from cuprum.pump_events import PumpEvent, RustPumpHandoffOutcome
from cuprum.pump_observation import observe_pump
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector


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


class _ReaderPreparationError(OSError):
    """Signal a reader descriptor that cannot be prepared."""

    def __init__(self) -> None:
        """Initialize the test double's stable diagnostic message."""
        super().__init__("reader descriptor cannot be prepared")


class _MetricsObserverError(RuntimeError):
    """Signal a metrics collector that fails while recording an outcome."""

    def __init__(self) -> None:
        """Initialize the test double's stable diagnostic message."""
        super().__init__("metrics unavailable")


@dc.dataclass(frozen=True, slots=True)
class _PreNativeFailureCase:
    """One bounded pre-native failure that preserves Python writer ownership."""

    target_symbol: str
    failure: type[OSError]
    error_match: str
    expected_outcome: RustPumpHandoffOutcome
    expected_phase: str
    expected_error_type: str


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


def _assert_handoff_metric(
    collector: RecordingCollector,
    expected: RustPumpHandoffOutcome,
    message: str,
) -> None:
    """Assert that one call incremented the expected bounded hand-off metric."""
    assert collector.counters == [
        (RUST_PUMP_HANDOFF_TOTAL, 1.0, {"outcome": expected})
    ], message


def _assert_handoff_failure_record(
    caplog: pytest.LogCaptureFixture,
    expected_phase: str,
    expected_error_type: str,
) -> None:
    """Assert that one bounded diagnostic records the given failure phase."""
    records = [
        record.__dict__
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "rust_pump_handoff_failed"
    ]
    assert len(records) == 1, "one hand-off failure must produce one diagnostic"
    assert records[0]["cuprum_phase"] == expected_phase, (
        "the diagnostic must identify the bounded failed phase"
    )
    assert records[0]["cuprum_error_type"] == expected_error_type, (
        "the diagnostic must preserve the error category"
    )


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


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _PreNativeFailureCase(
                target_symbol="_convert_fd_for_platform",
                failure=_ReaderPreparationError,
                error_match="cannot be prepared",
                expected_outcome=RustPumpHandoffOutcome.READER_PREPARATION_FAILED,
                expected_phase="reader_preparation",
                expected_error_type="_ReaderPreparationError",
            ),
            id="reader-preparation",
        ),
        pytest.param(
            _PreNativeFailureCase(
                target_symbol="_transfer_writer_fd_for_platform",
                failure=_WindowsTransferError,
                error_match="DuplicateHandle",
                expected_outcome=RustPumpHandoffOutcome.PLATFORM_WRITER_TRANSFER_FAILED,
                expected_phase="platform_writer_transfer",
                expected_error_type="_WindowsTransferError",
            ),
            id="platform-writer-transfer",
        ),
    ],
)
def test_pre_native_failure_emits_one_bounded_outcome(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    case: _PreNativeFailureCase,
) -> None:
    """Pre-native failures close Python's writer before Rust ownership."""
    caplog.set_level(logging.DEBUG, logger="cuprum._streams_rs")
    native_pump = mock.Mock(return_value=0)
    events, close_writer = _install_observed_native_pump(monkeypatch, native_pump)
    collector = RecordingCollector()

    def fail_pre_native_operation(resource: int) -> typ.NoReturn:
        """Fail before transferring the writer resource to Rust."""
        del resource
        raise case.failure

    monkeypatch.setattr(
        _streams_rs,
        case.target_symbol,
        fail_pre_native_operation,
    )
    with (
        observe_pump(events.append),
        observe_pump(PumpMetricsHook(collector)),
        pytest.raises(case.failure, match=case.error_match),
    ):
        _streams_rs.rust_pump_stream(11, 12)

    close_writer.assert_called_once_with(12)
    native_pump.assert_not_called()
    _assert_handoff_outcome(
        events,
        case.expected_outcome,
        f"{case.expected_phase} failure must emit exactly its closed outcome",
    )
    _assert_handoff_metric(
        collector,
        case.expected_outcome,
        f"{case.expected_phase} failure must increment one bounded hand-off metric",
    )
    _assert_handoff_failure_record(
        caplog,
        case.expected_phase,
        case.expected_error_type,
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
