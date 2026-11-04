"""Check deterministic reachability for writer-handoff terminal outcomes."""

from __future__ import annotations

import collections.abc as cabc

import pytest

from cuprum.unittests._rust_pump_writer_handoff_stateful_model import (
    _RustPumpWriterHandoffModel,
)

type _Scenario = cabc.Callable[[_RustPumpWriterHandoffModel], None]


def _apply(machine: _RustPumpWriterHandoffModel, name: str) -> None:
    """Apply the named transition only when the model currently enables it."""
    enabled = {
        transition.name: transition for transition in machine._enabled_transitions()
    }
    assert name in enabled, f"{name!r} is not enabled in stage {machine._stage!r}"
    enabled[name].apply()


def _prepare_submission(machine: _RustPumpWriterHandoffModel) -> None:
    """Run the Python-owned setup and cross the executor boundary."""
    _apply(machine, "duplicate creation")
    _apply(machine, "blocking-mode ready")
    _apply(machine, "executor submission")


def _finish_native_work(
    machine: _RustPumpWriterHandoffModel,
    *,
    fails: bool = False,
) -> None:
    """Finish native work and restore the asyncio writer in the model."""
    _apply(machine, "native load")
    _apply(machine, "buffer validation")
    _apply(machine, "writer transfer")
    _apply(machine, "native I/O failure sample 0" if fails else "native I/O success")
    _apply(machine, "Rust closes writer")
    _apply(machine, "settle worker")
    _apply(machine, "restore descriptors")
    _apply(machine, "resume reader")


def _finish_lifecycle(
    machine: _RustPumpWriterHandoffModel,
    *,
    fails: bool = False,
) -> None:
    """Finish native work, restore asyncio, and close its original writer."""
    _finish_native_work(machine, fails=fails)
    _apply(machine, "close original writer")


def _complete(machine: _RustPumpWriterHandoffModel) -> None:
    """Reach completion without cancellation or native I/O failure."""
    _prepare_submission(machine)
    _finish_lifecycle(machine)


def _complete_after_cancellation(machine: _RustPumpWriterHandoffModel) -> None:
    """Reach completion after one cancellation request."""
    _prepare_submission(machine)
    machine._cancel_before_native_settlement()
    _finish_lifecycle(machine)


def _duplicate_creation_failure(machine: _RustPumpWriterHandoffModel) -> None:
    """Fail before a Python-owned duplicate exists."""
    _apply(machine, "duplicate creation failure")


def _incomplete_lifecycle(machine: _RustPumpWriterHandoffModel) -> None:
    """Stop just after duplicate creation."""
    _apply(machine, "duplicate creation")


def _blocking_mode_failure(machine: _RustPumpWriterHandoffModel) -> None:
    """Fail while Python still owns the prepared duplicate."""
    _apply(machine, "duplicate creation")
    _apply(machine, "blocking-mode failure")


def _executor_rejection(machine: _RustPumpWriterHandoffModel) -> None:
    """Reject submission and close the Python-owned duplicate once."""
    _apply(machine, "duplicate creation")
    _apply(machine, "blocking-mode ready")
    _apply(machine, "executor rejection")


def _native_load_failure(machine: _RustPumpWriterHandoffModel) -> None:
    """Fail in the worker before loading the native pump."""
    _prepare_submission(machine)
    _apply(machine, "native load failure")


def _buffer_validation_failure(machine: _RustPumpWriterHandoffModel) -> None:
    """Fail in the worker after loading the callable but before native I/O."""
    _prepare_submission(machine)
    _apply(machine, "native load")
    _apply(machine, "buffer validation failure")


def _windows_transfer_failure(machine: _RustPumpWriterHandoffModel) -> None:
    """Fail in the Windows CRT-to-handle transfer path."""
    machine._choose_platform(is_windows=True)
    _prepare_submission(machine)
    _apply(machine, "native load")
    _apply(machine, "buffer validation")
    _apply(machine, "Windows transfer failure")


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
            _duplicate_creation_failure,
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
            _incomplete_lifecycle,
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

    _apply(machine, "native load")
    machine._assert_all_invariants()

    assert machine._stage == "native_loaded", (
        "worker progress must resume after the cancellation bound"
    )


def test_released_descriptor_slot_can_be_reused_after_settlement() -> None:
    """An unrelated owner can reuse the slot after Rust releases the writer."""
    machine = _RustPumpWriterHandoffModel()
    _prepare_submission(machine)
    _finish_native_work(machine)
    _apply(machine, "reuse released descriptor slot")
    _apply(machine, "close original writer")

    machine._assert_all_invariants()

    assert machine._reused_slot_open, "the released descriptor slot must be reusable"
