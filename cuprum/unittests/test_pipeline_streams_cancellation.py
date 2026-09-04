"""Cancelling an inter-stage hop while the Rust pump owns the descriptors.

``run_in_executor`` cannot interrupt the worker thread, so cancellation is the
one path where the awaiting task and the descriptor owner disagree about who is
finished. These tests pin both halves of that hand-back: the descriptors are
restored only after the worker returns, and the worker's own outcome is
consumed rather than left to resurface later.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import gc
import logging
import os
import sys
import threading
import types
import typing as typ

import pytest

from cuprum import (
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
)
from cuprum.pump_observation import observe_pump

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.pump_events import PumpEvent


class _RecordingGuard:
    """Guard double that records when the descriptors are restored."""

    def __init__(self, events: list[str]) -> None:
        """Record restores into the shared ``events`` log."""
        self.events = events

    def restore(self) -> None:
        """Record the restore so its ordering can be asserted."""
        self.events.append("restored")


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


@dc.dataclass
class _MidTransferContext:
    """Coordination objects shared by a cancellation-transfer test."""

    events: list[str]
    worker_started: threading.Event
    worker_finished: threading.Event
    release: threading.Event


class _FakeStreamsRs(types.ModuleType):
    """Fake ``cuprum._streams_rs`` module with the pump entry point."""

    rust_pump_stream: cabc.Callable[[int, int], int]


def _install_fake_pump(
    monkeypatch: pytest.MonkeyPatch,
    pump: cabc.Callable[[int, int], int],
) -> None:
    """Replace the Rust pump entry point with ``pump``."""
    fake_streams_rs = _FakeStreamsRs("cuprum._streams_rs")
    fake_streams_rs.rust_pump_stream = pump
    monkeypatch.setitem(sys.modules, "cuprum._streams_rs", fake_streams_rs)


async def _cancel_mid_transfer(
    context: _MidTransferContext,
    *,
    cancellations: int = 1,
) -> None:
    """Start a pipeline pump, cancel it mid-transfer, then release its worker."""
    reader_fd, writer_fd = os.pipe()
    try:
        state = _pipeline_stream_native_cleanup._RustPumpState(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            blocking_mode_guard=typ.cast(
                "_pipeline_stream_fds._BlockingModeGuard",
                _RecordingGuard(context.events),
            ),
            resume_reader=None,
        )
        task = asyncio.create_task(
            _pipeline_streams._run_rust_pump_with_blocking_fds(state=state)
        )
        started = await asyncio.to_thread(context.worker_started.wait, 5.0)
        assert started, (
            "the pump worker did not start within 5s, so cancellation would "
            "not be mid-transfer"
        )
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0.05)
        context.events.append("released")
        context.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.to_thread(context.worker_finished.wait, 5.0)
        await asyncio.sleep(0)
    finally:
        with contextlib.suppress(OSError):
            os.close(reader_fd)
        with contextlib.suppress(OSError):
            os.close(writer_fd)


@pytest.mark.parametrize(
    "cancellations",
    [
        pytest.param(1, id="single-cancellation"),
        pytest.param(3, id="repeated-cancellation-keeps-cleanup-worker-owned"),
    ],
)
def test_cancellation_restores_descriptors_only_after_worker_returns(
    monkeypatch: pytest.MonkeyPatch,
    cancellations: int,
) -> None:
    """A cancelled pipeline pump waits for the worker before restoring FDs.

    ``run_in_executor`` cannot interrupt the worker thread, which still owns
    both raw descriptors. Restoring their blocking mode while it runs would
    race it, so the ordering asserted here is the actual safety property.
    """
    events: list[str] = []
    release = threading.Event()
    worker_started = threading.Event()
    worker_finished = threading.Event()
    context = _MidTransferContext(
        events=events,
        worker_started=worker_started,
        worker_finished=worker_finished,
        release=release,
    )

    release_waits: list[bool] = []

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Block until released, standing in for an in-flight Rust transfer."""
        del reader_fd, writer_fd
        worker_started.set()
        release_waits.append(release.wait(timeout=5.0))
        events.append("worker_returned")
        worker_finished.set()
        return 0

    _install_fake_pump(monkeypatch, blocking_pump)
    asyncio.run(
        _cancel_mid_transfer(
            context,
            cancellations=cancellations,
        )
    )

    # `Event.wait` reports a timeout by returning False, so a worker that timed
    # out would still append "worker_returned" and satisfy the ordering below
    # without the drain ever having held it. Checked here rather than inside the
    # double because an assertion raised on the worker thread would only ever be
    # retrieved from the future and logged, never failing the test.
    assert release_waits == [True], (
        "the pump worker must return because it was released, not because its "
        f"5s wait timed out; observed waits {release_waits}"
    )
    assert "restored" in events, "the descriptors must still be restored"
    assert events.index("worker_returned") < events.index("restored"), (
        f"restore must happen only after the worker thread returns, even after "
        f"{cancellations} cancellation(s); observed order {events}"
    )
    assert events.count("restored") == 1, (
        "the reader descriptor state must be restored exactly once after "
        f"cancellation; observed order {events}"
    )


def test_cancellation_records_native_pump_cleanup_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancellation records bounded native-pump cleanup completion telemetry."""
    events: list[str] = []
    release = threading.Event()
    worker_started = threading.Event()
    worker_finished = threading.Event()
    context = _MidTransferContext(
        events=events,
        worker_started=worker_started,
        worker_finished=worker_finished,
        release=release,
    )
    pump_events: list[PumpEvent] = []

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Block until cancellation releases the native-pump stand-in."""
        del reader_fd, writer_fd
        worker_started.set()
        release.wait(timeout=5.0)
        worker_finished.set()
        return 0

    _install_fake_pump(monkeypatch, blocking_pump)
    caplog.set_level(logging.DEBUG, logger=_pipeline_streams.__name__)
    with observe_pump(pump_events.append):
        asyncio.run(_cancel_mid_transfer(context))

    assert [event.phase for event in pump_events] == [
        "cleanup_started",
        "cleanup_completed",
    ], f"cleanup telemetry must bracket cancellation, found {pump_events}"
    duration_s = pump_events[1].duration_s
    assert duration_s is not None, (
        f"cleanup completion must carry a monotonic duration, found {duration_s!r}"
    )
    assert duration_s >= 0.0, (
        "cleanup completion must carry a non-negative monotonic duration, "
        f"found {duration_s!r}"
    )
    cleanup_records = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "rust_pump_cleanup"
    ]
    assert [record.__dict__.get("cuprum_outcome") for record in cleanup_records] == [
        "started",
        "completed",
    ], f"cleanup logs must report both bounded outcomes, found {cleanup_records}"
    assert all(
        record.__dict__.get("cuprum_operation") == "native_pump_cleanup"
        for record in cleanup_records
    ), f"cleanup logs must name their operation, found {cleanup_records}"


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
    """Release a deferred worker and leave its callback one loop turn to run."""
    context.release.set()
    finished = await asyncio.to_thread(context.worker_finished.wait, 5.0)
    assert finished, "the test must release the native worker after caller return"
    await asyncio.sleep(0)
    with contextlib.suppress(OSError):
        os.close(reader_fd)
    with contextlib.suppress(OSError):
        os.close(writer_fd)


def test_grace_expiry_defers_worker_owned_descriptor_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grace expiry returns cancellation before a held worker releases FDs."""
    events: list[str] = []
    context = _MidTransferContext(
        events=events,
        worker_started=threading.Event(),
        worker_finished=threading.Event(),
        release=threading.Event(),
    )
    pump_events: list[PumpEvent] = []

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Hold native descriptors until the test explicitly releases them."""
        del reader_fd, writer_fd
        context.worker_started.set()
        context.release.wait(timeout=5.0)
        context.worker_finished.set()
        return 0

    _install_fake_pump(monkeypatch, blocking_pump)

    async def exercise() -> None:
        """Assert both the bounded caller result and eventual callback cleanup."""
        guard = _RecordingGuard(events)
        state, _task = await _cancel_until_cleanup_grace_expires(
            context,
            guard=guard,
        )
        assert state.was_deferred, "grace expiry must mark cleanup as deferred"
        assert events == [], (
            "worker-owned descriptors must not restore before late completion, "
            f"found {events}"
        )
        await _release_deferred_worker(
            context,
            reader_fd=state.reader_fd,
            writer_fd=state.writer_fd,
        )

    with observe_pump(pump_events.append):
        asyncio.run(exercise())

    assert events == ["restored"], (
        f"late completion must restore descriptor state exactly once, found {events}"
    )
    assert [event.phase for event in pump_events] == [
        "cleanup_started",
        "cleanup_grace_expired",
        "cleanup_deferred",
    ], f"grace expiry must defer the terminal callback, found {pump_events}"
    assert pump_events[1].elapsed_s is not None, (
        "grace expiry must report the bounded monotonic elapsed time"
    )


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

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Hold native descriptors until the caller grace has expired."""
        del reader_fd, writer_fd
        context.worker_started.set()
        context.release.wait(timeout=5.0)
        context.worker_finished.set()
        return 0

    _install_fake_pump(monkeypatch, blocking_pump)

    async def exercise() -> None:
        """Run the deferred cleanup through a restore failure."""
        guard = _FailingDeferredGuard(events, context.worker_finished)
        state, _task = await _cancel_until_cleanup_grace_expires(
            context,
            guard=guard,
        )
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


def _assert_cancelled_pump_failure_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Assert that a cancelled pump failure was retrieved and recorded."""
    unretrieved = [
        record.getMessage()
        for record in caplog.records
        if "never retrieved" in record.getMessage()
    ]
    assert not unretrieved, (
        "the pump's failure must be retrieved from the future, but asyncio "
        f"reported it as unhandled: {unretrieved}"
    )
    reported = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "rust_pump_failed_after_cancel"
    ]
    assert len(reported) == 1, (
        "a pump failure masked by cancellation must be recorded exactly once, "
        f"found {len(reported)}"
    )
    exc_info = reported[0].exc_info
    assert exc_info is not None, "the record must attach the pump exception"
    assert isinstance(exc_info[1], OSError), (
        f"the attached exception must be the pump's own, found {exc_info[1]!r}"
    )
    assert str(exc_info[1]) == "the pump failed while the hop was being cancelled", (
        f"the attached exception must carry the pump's message, found {exc_info[1]!r}"
    )


def test_a_failing_pump_on_a_cancelled_hop_reports_the_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pump failure on a cancelled hop is consumed, not left dangling.

    The caller is told about the cancellation, which is what it asked for. The
    worker's own failure still has to be retrieved from the future, or asyncio
    reports it as an unretrieved exception when the future is collected — long
    after the hop, attached to no useful context.
    """
    events: list[str] = []
    release = threading.Event()
    worker_started = threading.Event()
    worker_finished = threading.Event()
    context = _MidTransferContext(
        events=events,
        worker_started=worker_started,
        worker_finished=worker_finished,
        release=release,
    )

    release_waits: list[bool] = []

    def failing_pump(reader_fd: int, writer_fd: int) -> int:
        """Fail after the cancellation has been delivered."""
        del reader_fd, writer_fd
        worker_started.set()
        release_waits.append(release.wait(timeout=5.0))
        events.append("worker_returned")
        worker_finished.set()
        msg = "the pump failed while the hop was being cancelled"
        raise OSError(msg)

    _install_fake_pump(monkeypatch, failing_pump)

    with (
        caplog.at_level(logging.ERROR, logger="asyncio"),
        caplog.at_level(logging.DEBUG, logger="cuprum._pipeline_streams"),
    ):
        # CancelledError, not OSError: _cancel_mid_transfer asserts it.
        asyncio.run(
            _cancel_mid_transfer(
                context,
            )
        )
        # Force collection of the executor future while capture is still live.
        gc.collect()

    # A timed-out wait returns False rather than raising, so the pump would
    # still fail and still be reported — but after the cancellation had already
    # been drained, which is not the scenario under test. Checked on this thread
    # because an assertion on the worker would merely replace the OSError the
    # rest of the test inspects.
    assert release_waits == [True], (
        "the pump must fail because it was released mid-cancellation, not "
        f"because its 5s wait timed out; observed waits {release_waits}"
    )
    assert "restored" in events, "a failing pump must still restore the FDs"
    _assert_cancelled_pump_failure_reported(caplog)
