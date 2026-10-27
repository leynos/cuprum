"""Check deterministic reachability for writer-handoff terminal outcomes."""

from __future__ import annotations

import collections.abc as cabc

import pytest

from cuprum.unittests._rust_pump_writer_handoff_stateful_model import (
    _RustPumpWriterHandoffModel,
)

type _Scenario = cabc.Callable[[_RustPumpWriterHandoffModel], None]


def _prepare_submission(machine: _RustPumpWriterHandoffModel) -> None:
    """Run the Python-owned setup and cross the executor boundary."""
    machine._create_duplicate()
    machine._configure_duplicate_blocking_mode()
    machine._submit_executor()


def _finish_lifecycle(
    machine: _RustPumpWriterHandoffModel,
    *,
    fails: bool = False,
) -> None:
    """Finish native work and restore the asyncio writer in the model."""
    machine._load_native()
    machine._validate_buffer_size()
    machine._transfer_platform_writer()
    if fails:
        machine._invoke_native_pump_failure()
    else:
        machine._invoke_native_pump_success()
    machine._rust_closes_transferred_writer()
    machine._settle_native_worker()
    machine._restore_descriptors()
    machine._resume_reader()
    machine._close_original_writer()


def _complete(machine: _RustPumpWriterHandoffModel) -> None:
    """Reach completion without cancellation or native I/O failure."""
    _prepare_submission(machine)
    _finish_lifecycle(machine)


def _complete_after_cancellation(machine: _RustPumpWriterHandoffModel) -> None:
    """Reach completion after one cancellation request."""
    _prepare_submission(machine)
    machine._cancel_before_native_settlement()
    _finish_lifecycle(machine)


def _blocking_mode_failure(machine: _RustPumpWriterHandoffModel) -> None:
    """Fail while Python still owns the prepared duplicate."""
    machine._create_duplicate()
    machine._fail_blocking_mode()


def _executor_rejection(machine: _RustPumpWriterHandoffModel) -> None:
    """Reject submission and close the Python-owned duplicate once."""
    machine._create_duplicate()
    machine._configure_duplicate_blocking_mode()
    machine._reject_executor()


def _native_load_failure(machine: _RustPumpWriterHandoffModel) -> None:
    """Fail in the worker before loading the native pump."""
    _prepare_submission(machine)
    machine._fail_native_load()


def _buffer_validation_failure(machine: _RustPumpWriterHandoffModel) -> None:
    """Fail in the worker after loading the callable but before native I/O."""
    _prepare_submission(machine)
    machine._load_native()
    machine._fail_buffer_validation()


def _windows_transfer_failure(machine: _RustPumpWriterHandoffModel) -> None:
    """Fail in the Windows CRT-to-handle transfer path."""
    machine._choose_platform(is_windows=True)
    _prepare_submission(machine)
    machine._load_native()
    machine._validate_buffer_size()
    machine._fail_windows_transfer()


def _native_io_failure(machine: _RustPumpWriterHandoffModel) -> None:
    """Complete cleanup after one native I/O failure."""
    _prepare_submission(machine)
    _finish_lifecycle(machine, fails=True)


@pytest.mark.parametrize(
    ("label", "scenario"),
    [
        pytest.param("complete", _complete, id="complete"),
        pytest.param(
            "complete after cancellation",
            _complete_after_cancellation,
            id="complete-after-cancellation",
        ),
        pytest.param(
            "pre-submission failure: duplicate creation",
            _RustPumpWriterHandoffModel._fail_duplicate_creation,
            id="duplicate-creation-failure",
        ),
        pytest.param(
            "pre-submission failure: blocking-mode configuration",
            _blocking_mode_failure,
            id="blocking-mode-failure",
        ),
        pytest.param(
            "pre-submission failure: executor rejection",
            _executor_rejection,
            id="executor-rejection",
        ),
        pytest.param(
            "worker-side failure", _native_load_failure, id="native-load-failure"
        ),
        pytest.param(
            "worker-side failure",
            _buffer_validation_failure,
            id="buffer-validation-failure",
        ),
        pytest.param(
            "worker-side failure",
            _windows_transfer_failure,
            id="windows-transfer-failure",
        ),
        pytest.param("native I/O failure", _native_io_failure, id="native-io-failure"),
        pytest.param(
            "incomplete lifecycle",
            _RustPumpWriterHandoffModel._create_duplicate,
            id="incomplete-lifecycle",
        ),
    ],
)
def test_terminal_outcome_is_method_reachable(
    label: str,
    scenario: _Scenario,
) -> None:
    """Each terminal class has a deterministic sequence of enabled methods."""
    machine = _RustPumpWriterHandoffModel()
    scenario(machine)

    machine._assert_all_invariants()

    assert label in machine._terminal_labels(), (
        f"method sequence must reach terminal class {label!r}"
    )


def test_cancellation_retries_are_bounded_and_then_progress_resumes() -> None:
    """Two cancellation requests remain available before worker progress."""
    machine = _RustPumpWriterHandoffModel()
    _prepare_submission(machine)
    machine._cancel_before_native_settlement()
    machine._cancel_before_native_settlement()

    assert machine._cancellation_count == machine._MAX_CANCELLATIONS, (
        "the deterministic cancellation path must exercise the configured bound"
    )
    assert all(
        transition.name != "cancel" for transition in machine._enabled_transitions()
    ), "the model must stop offering cancellation at its bound"

    machine._load_native()
    machine._assert_all_invariants()


def test_released_descriptor_slot_can_be_reused_after_settlement() -> None:
    """An unrelated owner can reuse the slot after Rust releases the writer."""
    machine = _RustPumpWriterHandoffModel()
    _prepare_submission(machine)
    _finish_lifecycle(machine)
    machine._reuse_released_descriptor_slot()

    machine._assert_all_invariants()

    assert machine._reused_slot_open, "the released descriptor slot must be reusable"
