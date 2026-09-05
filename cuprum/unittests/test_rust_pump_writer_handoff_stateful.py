"""Pure state-machine checks for the Rust writer-descriptor ownership contract.

The model covers ownership transfer and cleanup order without native code or
threads. Fixed-case integration tests cover real descriptors, executor seams,
and Windows handle transfer.
"""

from __future__ import annotations

import typing as typ

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

type _Stage = typ.Literal[
    "new",
    "duplicated",
    "blocking_ready",
    "submitted",
    "native_loaded",
    "buffer_validated",
    "platform_ready",
    "native_finished",
    "worker_finished",
    "settled",
    "restored",
    "resumed",
    "complete",
    "pre_submission_failed",
]


class _RustPumpWriterHandoffMachine(RuleBasedStateMachine):
    """Model the one-way writer-resource transfer to a native pump worker."""

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
        self._was_cancelled = False
        self._native_settled = False
        self._reader_restored = False
        self._reader_resumed = False
        self._cleanup_complete = False
        self._asyncio_original_close_count = 0
        self._reused_slot_open = False

    @initialize(is_windows=st.booleans())
    def _choose_platform(self, is_windows: bool) -> None:
        """Choose the platform-specific writer-transfer path for this run."""
        self._is_windows = is_windows

    @precondition(lambda self: self._stage == "new")
    @rule(fails=st.booleans())
    def _create_duplicate(self, fails: bool) -> None:
        """Create the duplicate, or fail before any duplicate exists."""
        if fails:
            self._stage = "pre_submission_failed"
            return
        self._duplicate_owner = "python"
        self._stage = "duplicated"

    @precondition(lambda self: self._stage == "pre_submission_failed")
    @rule()
    def _retain_pre_submission_failure(self) -> None:
        """Keep a terminal setup failure available for bounded exploration."""

    @precondition(lambda self: self._stage == "duplicated")
    @rule(fails=st.booleans())
    def _configure_duplicate_blocking_mode(self, fails: bool) -> None:
        """Configure the Python-owned duplicate before executor submission."""
        if fails:
            self._close_duplicate_as_python()
            self._stage = "pre_submission_failed"
            return
        self._stage = "blocking_ready"

    @precondition(lambda self: self._stage == "blocking_ready")
    @rule(rejected=st.booleans())
    def _submit_executor(self, rejected: bool) -> None:
        """Reject or accept submission, which is the ownership boundary."""
        if rejected:
            self._close_duplicate_as_python()
            self._stage = "pre_submission_failed"
            return
        self._duplicate_owner = "rust"
        self._stage = "submitted"

    @precondition(lambda self: self._stage == "submitted")
    @rule(fails=st.booleans())
    def _load_native(self, fails: bool) -> None:
        """Load the native callable after the duplicate belongs to Rust's side."""
        if fails:
            self._close_duplicate_from_worker()
            self._stage = "worker_finished"
            return
        self._stage = "native_loaded"

    @precondition(lambda self: self._stage == "native_loaded")
    @rule(fails=st.booleans())
    def _validate_buffer_size(self, fails: bool) -> None:
        """Validate before Windows transfer, retaining worker-side ownership."""
        if fails:
            self._close_duplicate_from_worker()
            self._stage = "worker_finished"
            return
        self._stage = "buffer_validated"

    @precondition(lambda self: self._stage == "buffer_validated")
    @rule(fails=st.booleans())
    def _transfer_platform_writer(self, fails: bool) -> None:
        """Model Unix passthrough or the Windows CRT-to-handle transfer."""
        if self._is_windows and fails:
            self._close_duplicate_from_worker()
            self._stage = "worker_finished"
            return
        if self._is_windows:
            self._windows_crt_closed_by_shim += 1
            self._duplicate_owner = "closed"
        self._stage = "platform_ready"

    @precondition(lambda self: self._stage == "platform_ready")
    @rule(fails=st.booleans())
    def _invoke_native_pump(self, fails: bool) -> None:
        """Run the Rust pump once; both I/O outcomes retain Rust ownership."""
        self._native_invocations += 1
        self._native_io_failed = fails
        self._stage = "native_finished"

    @precondition(lambda self: self._stage == "native_finished")
    @rule()
    def _rust_closes_transferred_writer(self) -> None:
        """Release the resource Rust owns before worker settlement."""
        if self._is_windows:
            self._windows_handle_closed_by_rust += 1
        else:
            self._duplicate_closed_by_rust += 1
            self._duplicate_owner = "closed"
        self._stage = "worker_finished"

    @precondition(
        lambda self: (
            self._stage
            in {
                "submitted",
                "native_loaded",
                "buffer_validated",
                "platform_ready",
                "native_finished",
            }
            and not self._native_settled
        )
    )
    @rule()
    def _cancel_before_native_settlement(self) -> None:
        """Request cleanup without settling the worker itself."""
        self._was_cancelled = True

    @precondition(lambda self: self._was_cancelled and not self._native_settled)
    @rule()
    def _repeat_cancellation(self) -> None:
        """Keep pending native cleanup unchanged after repeated cancellation."""

    @precondition(lambda self: self._stage == "worker_finished")
    @rule()
    def _settle_native_worker(self) -> None:
        """Settle only after the worker has released its writer resource."""
        self._native_settled = True
        self._stage = "settled"

    @precondition(lambda self: self._stage == "settled")
    @rule()
    def _restore_descriptors(self) -> None:
        """Restore asyncio descriptor state after native settlement."""
        self._reader_restored = True
        self._stage = "restored"

    @precondition(lambda self: self._stage == "restored")
    @rule()
    def _resume_reader(self) -> None:
        """Resume asyncio only after descriptor restoration."""
        self._reader_resumed = True
        self._stage = "resumed"

    @precondition(
        lambda self: (
            self._stage == "resumed"
            and self._duplicate_owner == "closed"
            and not self._reused_slot_open
        )
    )
    @rule()
    def _reuse_released_descriptor_slot(self) -> None:
        """Allocate an unrelated external resource in Rust's released slot."""
        self._reused_slot_open = True

    @precondition(lambda self: self._stage == "resumed")
    @rule()
    def _close_original_writer_from_asyncio(self) -> None:
        """Close the original transport descriptor after native settlement."""
        self._asyncio_original_close_count += 1
        self._cleanup_complete = self._was_cancelled
        self._stage = "complete"

    @precondition(lambda self: self._stage == "complete")
    @rule()
    def _retain_completed_lifecycle(self) -> None:
        """Keep a terminal lifecycle available for bounded exploration."""

    def _close_duplicate_as_python(self) -> None:
        """Close only the duplicate while Python still owns it."""
        self._duplicate_closed_by_python += 1
        self._duplicate_owner = "closed"

    def _close_duplicate_from_worker(self) -> None:
        """Close a submitted duplicate from the worker-side shim path."""
        self._duplicate_closed_by_worker += 1
        self._duplicate_owner = "closed"

    @invariant()
    def _ownership_stays_on_its_side_of_submission(self) -> None:
        """Python owns only a pre-submission duplicate, and Rust owns after it."""
        if self._stage in {"duplicated", "blocking_ready"}:
            assert self._duplicate_owner == "python", (
                "the duplicate must remain Python-owned before submission"
            )
        if self._stage not in {"new", "pre_submission_failed"}:
            assert self._duplicate_closed_by_python <= 1, (
                "pre-submission rollback may close a duplicate only once"
            )
        if self._stage not in {
            "new",
            "duplicated",
            "blocking_ready",
            "pre_submission_failed",
        }:
            assert self._duplicate_closed_by_python == 0, (
                "Python must never close a duplicate after executor acceptance"
            )

    @invariant()
    def _pre_submission_failures_have_one_python_closer(self) -> None:
        """Blocking and submission rejection close exactly one Python resource."""
        if self._stage == "pre_submission_failed" and self._duplicate_owner == "closed":
            assert self._duplicate_closed_by_python == 1, (
                "a pre-submission duplicate failure needs exactly one Python close"
            )
        if self._stage == "pre_submission_failed" and self._duplicate_owner is None:
            assert self._duplicate_closed_by_python == 0, (
                "failed duplication creates no resource for Python to close"
            )

    @invariant()
    def _worker_failures_and_windows_transfer_do_not_invoke_native(self) -> None:
        """Pre-native worker failures close once and never reach Rust I/O."""
        if self._stage == "worker_finished" and self._native_invocations == 0:
            assert self._duplicate_closed_by_worker == 1, (
                "pre-native worker failure must close its resource once"
            )
        if self._is_windows and self._windows_crt_closed_by_shim:
            assert self._windows_crt_closed_by_shim == 1, (
                "the Windows CRT descriptor must transfer at most once"
            )

    @invariant()
    def _resource_closers_never_cross_ownership_boundaries(self) -> None:
        """Each resource has one closer and slot reuse remains external."""
        assert self._native_invocations <= 1, "the native pump must run at most once"
        if self._native_io_failed:
            assert self._native_invocations == 1, (
                "a native I/O failure can arise only from one native invocation"
            )
        assert self._duplicate_closed_by_rust <= 1, (
            "Rust must close its Unix duplicate at most once"
        )
        assert self._windows_handle_closed_by_rust <= 1, (
            "Rust must close its transferred Windows handle at most once"
        )
        assert self._asyncio_original_close_count <= 1, (
            "asyncio must close its original writer at most once"
        )
        if self._reused_slot_open:
            assert self._duplicate_closed_by_python == 0, (
                "Python cleanup must not close an externally reused descriptor slot"
            )
        if self._stage == "complete":
            assert self._asyncio_original_close_count == 1, (
                "terminal cleanup must have asyncio close the original writer"
            )

    @invariant()
    def _settlement_orders_transport_cleanup(self) -> None:
        """Restoration, resumption, and cancellation completion wait for settlement."""
        if self._reader_restored or self._reader_resumed:
            assert self._native_settled, (
                "transport restoration and resumption require native settlement"
            )
        if self._reader_resumed:
            assert self._reader_restored, (
                "reader resumption must follow descriptor restoration"
            )
        if self._cleanup_complete:
            assert self._was_cancelled, "cleanup completion requires cancellation"
            assert self._native_settled, (
                "cancelled cleanup cannot complete before native settlement"
            )


TestRustPumpWriterHandoffLifecycle = _RustPumpWriterHandoffMachine.TestCase
TestRustPumpWriterHandoffLifecycle.settings = settings(
    max_examples=60,
    stateful_step_count=16,
    deadline=None,
)
