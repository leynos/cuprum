"""Cancelling an inter-stage hop while the Rust pump owns the descriptors.

``run_in_executor`` cannot interrupt the worker thread, so cancellation is the
one path where the awaiting task and the descriptor owner disagree about who is
finished. These tests pin both halves of that hand-back: the descriptors are
restored only after the worker returns, and the worker's own outcome is
consumed rather than left to resurface later.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import sys
import threading
import types
import typing as typ

import pytest

from cuprum import _pipeline_stream_fds, _pipeline_streams

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _RecordingGuard:
    """Guard double that records when the descriptors are restored."""

    def __init__(self, events: list[str]) -> None:
        """Record restores into the shared ``events`` log."""
        self.events = events

    def restore(self) -> None:
        """Record the restore so its ordering can be asserted."""
        self.events.append("restored")


def _install_fake_pump(
    monkeypatch: pytest.MonkeyPatch,
    pump: cabc.Callable[[int, int], int],
) -> None:
    """Replace the Rust pump entry point with ``pump``."""
    fake_streams_rs = types.ModuleType("cuprum._streams_rs")
    fake_streams_rs.rust_pump_stream = pump  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cuprum._streams_rs", fake_streams_rs)


async def _cancel_mid_transfer(
    events: list[str],
    worker_started: threading.Event,
    worker_finished: threading.Event,
    release: threading.Event,
    *,
    cancellations: int = 1,
) -> None:
    """Start the pump, cancel it mid-transfer, then release the worker."""
    reader_fd, writer_fd = os.pipe()
    try:
        state = _pipeline_streams._RustPumpState(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            blocking_mode_guard=typ.cast(
                "_pipeline_stream_fds._BlockingModeGuard",
                _RecordingGuard(events),
            ),
            resume_reader=None,
        )
        task = asyncio.create_task(
            _pipeline_streams._run_rust_pump_with_blocking_fds(state=state)
        )
        await asyncio.to_thread(worker_started.wait, 5.0)
        for _ in range(cancellations):
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        events.append("released")
        release.set()
        await asyncio.to_thread(worker_finished.wait, 5.0)
        await asyncio.sleep(0)
    finally:
        os.close(reader_fd)
        os.close(writer_fd)


def test_cancellation_restores_descriptors_only_after_worker_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling mid-transfer waits for the worker before restoring FDs.

    ``run_in_executor`` cannot interrupt the worker thread, which still owns
    both raw descriptors. Restoring their blocking mode while it runs would
    race it, so the ordering asserted here is the actual safety property.
    """
    events: list[str] = []
    release = threading.Event()
    worker_started = threading.Event()
    worker_finished = threading.Event()

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Block until released, standing in for an in-flight Rust transfer."""
        del reader_fd, writer_fd
        worker_started.set()
        release.wait(timeout=5.0)
        events.append("worker_returned")
        worker_finished.set()
        return 0

    _install_fake_pump(monkeypatch, blocking_pump)
    asyncio.run(
        _cancel_mid_transfer(events, worker_started, worker_finished, release)
    )

    assert "restored" in events, "the descriptors must still be restored"
    assert events.index("worker_returned") < events.index("restored"), (
        "restore must happen only after the worker thread returns; observed "
        f"order {events}"
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

    def failing_pump(reader_fd: int, writer_fd: int) -> int:
        """Fail after the cancellation has been delivered."""
        del reader_fd, writer_fd
        worker_started.set()
        release.wait(timeout=5.0)
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
                events,
                worker_started,
                worker_finished,
                release,
            )
        )
        # Force collection of the executor future while capture is still live.
        gc.collect()

    assert "restored" in events, "a failing pump must still restore the FDs"
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
    assert "the pump failed while the hop was being cancelled" in (
        reported[0].getMessage()
    ), f"the record must carry the pump error, found {reported[0].getMessage()!r}"


def test_repeated_cancellation_keeps_cleanup_worker_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra cancellation requests cannot restore descriptors before the worker."""
    events: list[str] = []
    release = threading.Event()
    worker_started = threading.Event()
    worker_finished = threading.Event()

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Block until released, standing in for an in-flight Rust transfer."""
        del reader_fd, writer_fd
        worker_started.set()
        release.wait(timeout=5.0)
        events.append("worker_returned")
        worker_finished.set()
        return 0

    _install_fake_pump(monkeypatch, blocking_pump)
    asyncio.run(
        _cancel_mid_transfer(
            events,
            worker_started,
            worker_finished,
            release,
            cancellations=3,
        )
    )

    assert events.index("worker_returned") < events.index("restored"), (
        "repeated cancellation must not restore descriptors before the worker; "
        f"observed order {events}"
    )
