"""Rust-pump cleanup failures during cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import threading
import typing as typ

import pytest

from cuprum import (
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
)
from cuprum._pipeline_stream_fds import RUST_PUMP_TEARDOWN_FAILED_ACTION
from cuprum.unittests._rust_pump_test_helpers import install_fake_pump, owned_fds

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _FailingRestoreGuard:
    """Guard double that fails while returning descriptor ownership to asyncio."""

    def restore(self) -> None:
        """Fail with the descriptor error the cancellation path must suppress."""
        msg = "the descriptor cannot be restored"
        raise OSError(msg)


@dataclasses.dataclass
class _CancellationState:
    """Synchronization state for one cancelled native-pump test."""

    worker_started: threading.Event = dataclasses.field(default_factory=threading.Event)
    worker_finished: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )
    release: threading.Event = dataclasses.field(default_factory=threading.Event)
    cleanup_futures: list[asyncio.Future[None]] = dataclasses.field(
        default_factory=list
    )


async def _cancel_pump_with_failing_restore(
    state: _CancellationState,
) -> None:
    """Cancel a hop and prove it settles within the default grace period."""
    task: asyncio.Task[None] | None = None
    try:
        with owned_fds() as (reader_fd, writer_fd):
            pump_state = _pipeline_stream_native_cleanup._RustPumpState(
                reader_fd=reader_fd,
                writer_fd=writer_fd,
                blocking_mode_guard=typ.cast(
                    "_pipeline_stream_fds._BlockingModeGuard",
                    _FailingRestoreGuard(),
                ),
                resume_reader=None,
            )
            task = asyncio.create_task(
                _pipeline_streams._run_rust_pump_with_blocking_fds(state=pump_state)
            )
            started = await asyncio.to_thread(state.worker_started.wait, 5.0)
            assert started, "the native-pump stand-in must start before cancellation"
            task.cancel()
            await asyncio.sleep(0)
            state.release.set()
            done, pending = await asyncio.wait((task,), timeout=0.5)
            assert done == {task}, (
                "a cancelled hop must settle within the default cancellation grace "
                f"after restore failure; pending={pending}"
            )
            with pytest.raises(asyncio.CancelledError):
                task.result()
            finished = await asyncio.to_thread(state.worker_finished.wait, 5.0)
            assert finished, "the native-pump worker must settle before the hop"
    finally:
        for cleanup_complete in state.cleanup_futures:
            if not cleanup_complete.done():
                cleanup_complete.set_result(None)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _run_cancelled_pump_with_captured_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    state: _CancellationState,
) -> None:
    """Install the blocked pump double and run its cancellation path."""

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Block until cancellation releases the native-pump stand-in."""
        del reader_fd, writer_fd
        state.worker_started.set()
        state.release.wait(timeout=5.0)
        state.worker_finished.set()
        return 0

    original_await_cleanup = _pipeline_stream_native_cleanup._await_native_pump_cleanup
    capture_state = state

    async def capture_cleanup_future(
        cleanup_complete: asyncio.Future[None],
        *,
        monotonic_clock: cabc.Callable[[], float],
        cleanup_grace_s: float,
        state: _pipeline_stream_native_cleanup._RustPumpState | None = None,
    ) -> None:
        """Retain cleanup state so a broken implementation remains bounded."""
        capture_state.cleanup_futures.append(cleanup_complete)
        await original_await_cleanup(
            cleanup_complete,
            monotonic_clock=monotonic_clock,
            cleanup_grace_s=cleanup_grace_s,
            state=state,
        )

    install_fake_pump(monkeypatch, blocking_pump)
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_await_native_pump_cleanup",
        capture_cleanup_future,
    )
    asyncio.run(_cancel_pump_with_failing_restore(state))


def test_cancellation_settles_when_descriptor_restore_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed descriptor restore cannot strand a cancelled native pump."""
    state = _CancellationState()

    with caplog.at_level(logging.DEBUG, logger=_pipeline_streams.__name__):
        _run_cancelled_pump_with_captured_cleanup(monkeypatch, state)

    teardown_records = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == RUST_PUMP_TEARDOWN_FAILED_ACTION
    ]
    assert len(teardown_records) == 1, (
        "a failed descriptor restore must be recorded once, found "
        f"{len(teardown_records)}"
    )
    assert teardown_records[0].__dict__.get("cuprum_site") == "restore_state", (
        "the cleanup record must name state restoration, found "
        f"{teardown_records[0].__dict__.get('cuprum_site')!r}"
    )
    assert teardown_records[0].__dict__.get("cuprum_error_type") == "OSError", (
        "the cleanup record must preserve the restore failure type, found "
        f"{teardown_records[0].__dict__.get('cuprum_error_type')!r}"
    )
