"""State invariants for the Rust writer hand-off model."""

from __future__ import annotations

import typing as typ

from hypothesis.stateful import invariant

if typ.TYPE_CHECKING:
    from cuprum.unittests._rust_pump_writer_handoff_stateful_types import _Stage


def _require_invariant(message: str, *, condition: bool) -> None:
    """Raise an assertion failure when a model invariant does not hold."""
    if not condition:
        raise AssertionError(message)


class _WriterHandoffInvariantMixin:
    """Check ownership, failure cleanup, and cancellation ordering."""

    _is_windows: bool
    _stage: _Stage
    _duplicate_owner: typ.Literal["python", "rust", "closed"] | None
    _duplicate_closed_by_python: int
    _duplicate_closed_by_worker: int
    _duplicate_closed_by_rust: int
    _windows_handle_closed_by_rust: int
    _windows_crt_closed_by_shim: int
    _native_invocations: int
    _native_io_failed: bool
    _asyncio_original_close_count: int
    _reused_slot_open: bool
    _cancellation_count: int
    _MAX_CANCELLATIONS: int
    _was_cancelled: bool
    _native_settled: bool
    _reader_restored: bool
    _reader_resumed: bool
    _cleanup_complete: bool

    @invariant()
    def _ownership_stays_on_its_side_of_submission(self) -> None:
        """Python owns only a pre-submission duplicate, and Rust owns after it."""
        if self._stage in {"duplicated", "blocking_ready"}:
            _require_invariant(
                "the duplicate must remain Python-owned before submission",
                condition=self._duplicate_owner == "python",
            )
        if self._stage not in {"new", "pre_submission_failed"}:
            _require_invariant(
                "pre-submission rollback may close a duplicate only once",
                condition=self._duplicate_closed_by_python <= 1,
            )
        if self._stage not in {
            "new",
            "duplicated",
            "blocking_ready",
            "pre_submission_failed",
        }:
            _require_invariant(
                "Python must never close a duplicate after executor acceptance",
                condition=self._duplicate_closed_by_python == 0,
            )

    @invariant()
    def _pre_submission_failures_have_one_python_closer(self) -> None:
        """Blocking and submission rejection close exactly one Python resource."""
        if self._stage == "pre_submission_failed" and self._duplicate_owner == "closed":
            _require_invariant(
                "a pre-submission duplicate failure needs exactly one Python close",
                condition=self._duplicate_closed_by_python == 1,
            )
        if self._stage == "pre_submission_failed" and self._duplicate_owner is None:
            _require_invariant(
                "failed duplication creates no resource for Python to close",
                condition=self._duplicate_closed_by_python == 0,
            )

    @invariant()
    def _worker_failures_and_windows_transfer_do_not_invoke_native(self) -> None:
        """Pre-native worker failures close once and never reach Rust I/O."""
        if self._stage == "worker_finished" and self._native_invocations == 0:
            _require_invariant(
                "pre-native worker failure must close its resource once",
                condition=self._duplicate_closed_by_worker == 1,
            )
        if self._is_windows and self._windows_crt_closed_by_shim:
            _require_invariant(
                "the Windows CRT descriptor must transfer at most once",
                condition=self._windows_crt_closed_by_shim == 1,
            )

    @invariant()
    def _resource_closers_never_cross_ownership_boundaries(self) -> None:
        """Each resource has one closer and slot reuse remains external."""
        _require_invariant(
            "the native pump must run at most once",
            condition=self._native_invocations <= 1,
        )
        if self._native_io_failed:
            _require_invariant(
                "a native I/O failure can arise only from one native invocation",
                condition=self._native_invocations == 1,
            )
        _require_invariant(
            "Rust must close its Unix duplicate at most once",
            condition=self._duplicate_closed_by_rust <= 1,
        )
        _require_invariant(
            "Rust must close its transferred Windows handle at most once",
            condition=self._windows_handle_closed_by_rust <= 1,
        )
        _require_invariant(
            "asyncio must close its original writer at most once",
            condition=self._asyncio_original_close_count <= 1,
        )
        if self._reused_slot_open:
            _require_invariant(
                "Python cleanup must not close an externally reused descriptor slot",
                condition=self._duplicate_closed_by_python == 0,
            )
        if self._stage == "complete":
            _require_invariant(
                "terminal cleanup must have asyncio close the original writer",
                condition=self._asyncio_original_close_count == 1,
            )

    @invariant()
    def _settlement_orders_transport_cleanup(self) -> None:
        """Restoration, resumption, and cancellation completion wait for settlement."""
        _require_invariant(
            "cancellation requests must stay within the configured bound",
            condition=self._cancellation_count <= self._MAX_CANCELLATIONS,
        )
        _require_invariant(
            "cancellation state must match the number of requests",
            condition=self._was_cancelled == (self._cancellation_count > 0),
        )
        if self._reader_restored or self._reader_resumed:
            _require_invariant(
                "transport restoration and resumption require native settlement",
                condition=self._native_settled,
            )
        if self._reader_resumed:
            _require_invariant(
                "reader resumption must follow descriptor restoration",
                condition=self._reader_restored,
            )
        if self._cleanup_complete:
            _require_invariant(
                "cleanup completion requires cancellation",
                condition=self._was_cancelled,
            )
            _require_invariant(
                "cancelled cleanup cannot complete before native settlement",
                condition=self._native_settled,
            )

    def _assert_all_invariants(self) -> None:
        """Check the five ownership invariant groups in deterministic examples."""
        self._ownership_stays_on_its_side_of_submission()
        self._pre_submission_failures_have_one_python_closer()
        self._worker_failures_and_windows_transfer_do_not_invoke_native()
        self._resource_closers_never_cross_ownership_boundaries()
        self._settlement_orders_transport_cleanup()
