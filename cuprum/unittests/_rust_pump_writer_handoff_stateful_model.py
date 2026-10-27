"""Model writer ownership, cleanup order, and terminal outcomes."""

from __future__ import annotations

import typing as typ

from hypothesis import event
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule

from cuprum.unittests._rust_pump_writer_handoff_stateful_invariants import (
    _WriterHandoffInvariantMixin,
)
from cuprum.unittests._rust_pump_writer_handoff_stateful_types import (
    _Stage,
    _Transition,
)

if typ.TYPE_CHECKING:
    from hypothesis.strategies import DataObject


class _RustPumpWriterHandoffModel(_WriterHandoffInvariantMixin, RuleBasedStateMachine):
    """Track the one-way transfer of an asyncio writer to the native worker."""

    _MAX_CANCELLATIONS = 2

    def __init__(self) -> None:
        """Start with the original writer owned solely by asyncio."""
        super().__init__()
        self._is_windows = False
        self._stage: _Stage = "new"
        self._duplicate_owner: typ.Literal["python", "rust", "closed"] | None = None
        self._duplicate_closed_by_python = 0
        self._duplicate_closed_by_worker = 0
        self._duplicate_closed_by_rust = 0
        self._windows_handle_closed_by_rust = 0
        self._windows_crt_closed_by_shim = 0
        self._native_invocations = 0
        self._native_io_failed = False
        self._worker_side_failed = False
        self._pre_submission_failure_cause: str | None = None
        self._cancellation_count = 0
        self._was_cancelled = False
        self._native_settled = False
        self._reader_restored = False
        self._reader_resumed = False
        self._cleanup_complete = False
        self._asyncio_original_close_count = 0
        self._reused_slot_open = False

    @initialize(is_windows=st.booleans())
    def _choose_platform(self, *, is_windows: bool) -> None:
        """Choose the platform-specific writer-transfer path for this run."""
        self._is_windows = is_windows

    def _can_cancel(self) -> bool:
        """Return whether cancellation can still be requested before settlement."""
        return (
            self._stage
            in {
                "submitted",
                "native_loaded",
                "buffer_validated",
                "platform_ready",
                "native_finished",
            }
            and not self._native_settled
            and self._cancellation_count < self._MAX_CANCELLATIONS
        )

    def _with_cancellation(
        self,
        transitions: tuple[_Transition, ...],
    ) -> tuple[_Transition, ...]:
        """Offer bounded repeated cancellation while native work is unsettled."""
        if self._can_cancel():
            return (
                *transitions,
                _Transition("cancel", self._cancel_before_native_settlement),
            )
        return transitions

    def _enabled_transitions(self) -> tuple[_Transition, ...]:
        """Return only operations valid in the current lifecycle stage."""
        if self._stage in {"new", "duplicated", "blocking_ready"}:
            return self._pre_submission_transitions()
        if self._stage in {
            "submitted",
            "native_loaded",
            "buffer_validated",
            "platform_ready",
        }:
            return self._native_transitions()
        if self._stage in {
            "native_finished",
            "worker_finished",
            "settled",
            "restored",
            "resumed",
        }:
            return self._cleanup_transitions()
        return (
            _Transition("retain terminal lifecycle", self._retain_terminal_lifecycle),
        )

    def _pre_submission_transitions(self) -> tuple[_Transition, ...]:
        """Return setup transitions before ownership moves to the worker."""
        if self._stage == "new":
            return (
                _Transition("duplicate creation", self._create_duplicate),
                _Transition(
                    "duplicate creation failure", self._fail_duplicate_creation
                ),
            )
        if self._stage == "duplicated":
            return (
                _Transition(
                    "blocking-mode ready", self._configure_duplicate_blocking_mode
                ),
                _Transition("blocking-mode failure", self._fail_blocking_mode),
            )
        if self._stage == "blocking_ready":
            return (
                _Transition("executor submission", self._submit_executor),
                _Transition("executor rejection", self._reject_executor),
            )
        msg = f"unsupported pre-submission stage: {self._stage}"
        raise AssertionError(msg)

    def _native_transitions(self) -> tuple[_Transition, ...]:
        """Return enabled transitions while the worker owns the duplicate."""
        if self._stage == "submitted":
            return self._with_cancellation((
                _Transition("native load", self._load_native),
                _Transition("native load failure", self._fail_native_load),
            ))
        if self._stage == "native_loaded":
            return self._with_cancellation((
                _Transition("buffer validation", self._validate_buffer_size),
                _Transition("buffer validation failure", self._fail_buffer_validation),
            ))
        if self._stage == "buffer_validated":
            transitions = (
                (
                    _Transition("writer transfer", self._transfer_platform_writer),
                    _Transition(
                        "Windows transfer failure", self._fail_windows_transfer
                    ),
                )
                if self._is_windows
                else (_Transition("writer transfer", self._transfer_platform_writer),)
            )
            return self._with_cancellation(transitions)
        if self._stage == "platform_ready":
            io_failures = tuple(
                _Transition(
                    f"native I/O failure sample {sample}",
                    self._invoke_native_pump_failure,
                )
                for sample in range(8)
            )
            # Distinct choices bias the fixed example budget toward this branch.
            return self._with_cancellation((
                _Transition("native I/O success", self._invoke_native_pump_success),
                *io_failures,
            ))
        msg = f"unsupported worker stage: {self._stage}"
        raise AssertionError(msg)

    def _cleanup_transitions(self) -> tuple[_Transition, ...]:
        """Return enabled transitions after worker execution has finished."""
        if self._stage == "native_finished":
            return self._with_cancellation((
                _Transition("Rust closes writer", self._rust_closes_transferred_writer),
            ))
        if self._stage == "worker_finished":
            return (_Transition("settle worker", self._settle_native_worker),)
        if self._stage == "settled":
            return (_Transition("restore descriptors", self._restore_descriptors),)
        if self._stage == "restored":
            return (_Transition("resume reader", self._resume_reader),)
        if self._stage == "resumed":
            transitions = [
                _Transition("close original writer", self._close_original_writer)
            ]
            if self._duplicate_owner == "closed" and not self._reused_slot_open:
                transitions.append(
                    _Transition(
                        "reuse released descriptor slot",
                        self._reuse_released_descriptor_slot,
                    )
                )
            return tuple(transitions)
        msg = f"unsupported cleanup stage: {self._stage}"
        raise AssertionError(msg)

    @rule(data=st.data())
    def advance(self, data: DataObject) -> None:
        """Select and apply one transition that is enabled in the current stage."""
        transition = data.draw(st.sampled_from(self._enabled_transitions()))
        transition.apply()

    def _create_duplicate(self) -> None:
        """Create the duplicate while Python still owns it."""
        self._duplicate_owner = "python"
        self._stage = "duplicated"

    def _fail_duplicate_creation(self) -> None:
        """Record the setup failure that occurs before a duplicate exists."""
        self._pre_submission_failure_cause = "duplicate creation"
        self._stage = "pre_submission_failed"

    def _configure_duplicate_blocking_mode(self) -> None:
        """Finish Python-owned preparation before executor submission."""
        self._stage = "blocking_ready"

    def _fail_blocking_mode(self) -> None:
        """Roll back a duplicate when blocking-mode configuration fails."""
        self._pre_submission_failure_cause = "blocking-mode configuration"
        self._close_duplicate_as_python()
        self._stage = "pre_submission_failed"

    def _submit_executor(self) -> None:
        """Transfer duplicate ownership at successful executor submission."""
        self._duplicate_owner = "rust"
        self._stage = "submitted"

    def _reject_executor(self) -> None:
        """Roll back the Python-owned duplicate after executor rejection."""
        self._pre_submission_failure_cause = "executor rejection"
        self._close_duplicate_as_python()
        self._stage = "pre_submission_failed"

    def _load_native(self) -> None:
        """Load the native callable while the worker owns the duplicate."""
        self._stage = "native_loaded"

    def _fail_native_load(self) -> None:
        """Close the worker-owned duplicate when native loading fails."""
        self._finish_worker_failure()

    def _validate_buffer_size(self) -> None:
        """Advance after a valid buffer-size check."""
        self._stage = "buffer_validated"

    def _fail_buffer_validation(self) -> None:
        """Close the worker-owned duplicate when buffer validation fails."""
        self._finish_worker_failure()

    def _transfer_platform_writer(self) -> None:
        """Transfer the writer using the platform-specific ownership path."""
        if self._is_windows:
            self._windows_crt_closed_by_shim += 1
            self._duplicate_owner = "closed"
        self._stage = "platform_ready"

    def _fail_windows_transfer(self) -> None:
        """Close the worker-owned duplicate when Windows handle transfer fails."""
        self._finish_worker_failure()

    def _finish_worker_failure(self) -> None:
        """Record one pre-I/O failure and release the worker-owned duplicate."""
        self._worker_side_failed = True
        self._close_duplicate_from_worker()
        self._stage = "worker_finished"

    def _invoke_native_pump_success(self) -> None:
        """Record one successful native-pump invocation."""
        self._invoke_native_pump(fails=False)

    def _invoke_native_pump_failure(self) -> None:
        """Record one native-pump invocation that encounters I/O failure."""
        self._invoke_native_pump(fails=True)

    def _invoke_native_pump(self, *, fails: bool) -> None:
        """Record the native call outcome before Rust releases its resource."""
        self._native_invocations += 1
        self._native_io_failed = fails
        self._stage = "native_finished"

    def _rust_closes_transferred_writer(self) -> None:
        """Release the writer resource Rust owns before worker settlement."""
        if self._is_windows:
            self._windows_handle_closed_by_rust += 1
        else:
            self._duplicate_closed_by_rust += 1
            self._duplicate_owner = "closed"
        self._stage = "worker_finished"

    def _cancel_before_native_settlement(self) -> None:
        """Request bounded cleanup without settling the worker itself."""
        self._cancellation_count += 1
        self._was_cancelled = True

    def _settle_native_worker(self) -> None:
        """Settle only after the worker has released its writer resource."""
        self._native_settled = True
        self._stage = "settled"

    def _restore_descriptors(self) -> None:
        """Restore asyncio descriptor state after native settlement."""
        self._reader_restored = True
        self._stage = "restored"

    def _resume_reader(self) -> None:
        """Resume asyncio only after descriptor restoration."""
        self._reader_resumed = True
        self._stage = "resumed"

    def _reuse_released_descriptor_slot(self) -> None:
        """Allocate an unrelated external resource in Rust's released slot."""
        self._reused_slot_open = True

    def _close_original_writer(self) -> None:
        """Close the asyncio-owned original writer after native settlement."""
        self._asyncio_original_close_count += 1
        self._cleanup_complete = self._was_cancelled
        self._stage = "complete"

    def _retain_terminal_lifecycle(self) -> None:
        """Keep a terminal state available for bounded Hypothesis exploration."""

    def _close_duplicate_as_python(self) -> None:
        """Close a duplicate while Python still owns it."""
        self._duplicate_closed_by_python += 1
        self._duplicate_owner = "closed"

    def _close_duplicate_from_worker(self) -> None:
        """Close a submitted duplicate from the worker-side path."""
        self._duplicate_closed_by_worker += 1
        self._duplicate_owner = "closed"

    def _terminal_labels(self) -> tuple[str, ...]:
        """Return every terminal outcome observed by this lifecycle."""
        labels: list[str] = []
        if self._stage == "complete":
            labels.append(
                "complete after cancellation" if self._was_cancelled else "complete"
            )
        else:
            labels.append("incomplete lifecycle")
        if self._pre_submission_failure_cause is not None:
            labels.append(
                f"pre-submission failure: {self._pre_submission_failure_cause}"
            )
        if self._worker_side_failed:
            labels.append("worker-side failure")
        if self._native_io_failed:
            labels.append("native I/O failure")
        return tuple(labels)

    def teardown(self) -> None:
        """Record terminal and failure coverage for this generated lifecycle."""
        for label in self._terminal_labels():
            event(label)
