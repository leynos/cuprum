"""Deferred native-pump cleanup after cancellation grace expiry."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import typing as typ
from unittest import mock

import pytest

from cuprum import (
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
)
from cuprum.pump_observation import observe_pump
from cuprum.unittests.test_pipeline_streams_cancellation import (
    _install_fake_pump,
    _MidTransferContext,
    _RecordingGuard,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.pump_events import PumpEvent


class _FailingDeferredGuard:
    """Guard double that records a deferred restore failure."""

    def __init__(self, events: list[str], worker_finished: threading.Event) -> None:
        """Retain the order evidence needed for the deferred callback test."""
        self.events = events
        self.worker_finished = worker_finished

    def restore(self) -> None:
        """Fail only after the delayed worker has stopped using descriptors."""
        assert self.worker_finished.is_set(), (
            "deferred restoration must wait for native worker completion"
        )
        self.events.append("restore_failed")
        msg = "the deferred descriptor restore failed"
        raise OSError(msg)


async def _cancel_until_cleanup_grace_expires(
    context: _MidTransferContext,
    *,
    guard: _RecordingGuard | _FailingDeferredGuard,
) -> tuple[_pipeline_stream_native_cleanup._RustPumpState, asyncio.Task[None]]:
    """Return caller cancellation before releasing a held native worker."""
    reader_fd, writer_fd = os.pipe()
    state = _pipeline_stream_native_cleanup._RustPumpState(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
        blocking_mode_guard=typ.cast("_pipeline_stream_fds._BlockingModeGuard", guard),
        resume_reader=None,
        cleanup_grace_s=0.01,
    )
    task = asyncio.create_task(
        _pipeline_streams._run_rust_pump_with_blocking_fds(state=state)
    )
    started = await asyncio.to_thread(context.worker_started.wait, 5.0)
    assert started, "the native worker must start before grace expiry is exercised"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not context.worker_finished.is_set(), (
        "caller cancellation must return at grace expiry before worker completion"
    )
    return state, task


async def _release_deferred_worker(
    context: _MidTransferContext,
    *,
    reader_fd: int,
    writer_fd: int,
) -> None:
    """Release a deferred worker and wait for its callback to finalize cleanup."""
    context.release.set()
    finished = await asyncio.to_thread(context.worker_finished.wait, 5.0)
    assert finished, "the test must release the native worker after caller return"
    await _wait_for_native_pump_cleanup()
    with contextlib.suppress(OSError):
        os.close(reader_fd)
    with contextlib.suppress(OSError):
        os.close(writer_fd)


async def _wait_for_native_pump_cleanup() -> None:
    """Wait until the worker callback has discarded its retained future."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while _pipeline_stream_native_cleanup._NATIVE_PUMP_FUTURES:
        if loop.time() >= deadline:
            pytest.fail("the deferred native-pump completion callback did not finish")
        await asyncio.sleep(0.01)


def _blocking_pump(
    context: _MidTransferContext,
    native_fds: list[tuple[int, int]],
) -> cabc.Callable[[int, int], int]:
    """Build a worker that retains both native descriptors until release."""

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Hold native descriptors until the test explicitly releases them."""
        native_fds.append((reader_fd, writer_fd))
        context.worker_started.set()
        if not context.release.wait(timeout=5.0):
            context.release.wait()
        os.close(writer_fd)
        context.worker_finished.set()
        return 0

    return blocking_pump


def test_grace_expiry_defers_worker_owned_descriptor_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grace expiry keeps both native descriptors valid until worker completion."""
    events: list[str] = []
    context = _MidTransferContext(
        events=events,
        worker_started=threading.Event(),
        worker_finished=threading.Event(),
        release=threading.Event(),
    )
    pump_events: list[PumpEvent] = []
    native_fds: list[tuple[int, int]] = []
    close_native_reader = mock.Mock(
        wraps=_pipeline_stream_native_cleanup._close_rust_reader_fd
    )
    _install_fake_pump(monkeypatch, _blocking_pump(context, native_fds))
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_close_rust_reader_fd",
        close_native_reader,
    )

    async def exercise() -> None:
        """Assert the bounded result and deferred descriptor ownership."""
        guard = _RecordingGuard(events)
        state, _task = await _cancel_until_cleanup_grace_expires(context, guard=guard)
        assert state.was_deferred, "grace expiry must mark cleanup as deferred"
        assert events == [], "worker-owned descriptors must not restore early"
        assert len(native_fds) == 1, "the worker must receive both native FDs"
        native_reader_fd, native_writer_fd = native_fds[0]
        os.fstat(native_reader_fd)
        os.fstat(native_writer_fd)
        await _release_deferred_worker(
            context,
            reader_fd=state.reader_fd,
            writer_fd=state.writer_fd,
        )
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(native_reader_fd)
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(native_writer_fd)
        assert close_native_reader.call_args_list == [mock.call(native_reader_fd)], (
            "the callback must close its borrowed native reader exactly once"
        )

    with observe_pump(pump_events.append):
        asyncio.run(exercise())

    assert events == ["restored"], (
        f"late completion must restore descriptor state exactly once, found {events}"
    )
    assert [event.phase for event in pump_events] == [
        "handoff",
        "cleanup_started",
        "cleanup_grace_expired",
        "cleanup_deferred",
    ], f"grace expiry must defer the terminal callback, found {pump_events}"


def test_deferred_callback_suppresses_descriptor_restore_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A late callback retains cancellation semantics if restoration fails."""
    events: list[str] = []
    context = _MidTransferContext(
        events=events,
        worker_started=threading.Event(),
        worker_finished=threading.Event(),
        release=threading.Event(),
    )
    _install_fake_pump(monkeypatch, _blocking_pump(context, []))

    async def exercise() -> None:
        """Run the deferred cleanup through a restore failure."""
        guard = _FailingDeferredGuard(events, context.worker_finished)
        state, _task = await _cancel_until_cleanup_grace_expires(context, guard=guard)
        await _release_deferred_worker(
            context,
            reader_fd=state.reader_fd,
            writer_fd=state.writer_fd,
        )

    with caplog.at_level(logging.DEBUG, logger=_pipeline_streams.__name__):
        asyncio.run(exercise())

    assert events == ["restore_failed"], (
        f"deferred callback must attempt exactly one restore, found {events}"
    )
    assert any(
        record.__dict__.get("cuprum_site") == "restore_state"
        for record in caplog.records
    ), "deferred restore failure must retain teardown diagnostics"
