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
import threading

import pytest

from cuprum.unittests._rust_pump_test_helpers import (
    cancel_mid_transfer,
    install_fake_pump,
)


class _RecordingGuard:
    """Guard double that records when the descriptors are restored."""

    def __init__(self, events: list[str]) -> None:
        """Record restores into the shared ``events`` log."""
        self.events = events

    def restore(self) -> None:
        """Record the restore so its ordering can be asserted."""
        self.events.append("restored")


@pytest.mark.parametrize(
    "cancellations",
    [
        pytest.param(1, id="single-cancellation"),
        # A cancellation delivered *during* the drain interrupts the wait
        # itself. If that ended the drain, the descriptors would be restored
        # while the worker thread still owned them — the same race the single
        # cancellation guards, reachable by any caller that cancels twice.
        pytest.param(3, id="repeated-cancellation-cannot-cut-the-drain-short"),
    ],
)
def test_cancellation_restores_descriptors_only_after_worker_returns(
    monkeypatch: pytest.MonkeyPatch,
    cancellations: int,
) -> None:
    """Cancelling mid-transfer waits for the worker before restoring FDs.

    ``run_in_executor`` cannot interrupt the worker thread, which still owns
    both raw descriptors. Restoring their blocking mode while it runs would
    race it, so the ordering asserted here is the actual safety property.
    """
    events: list[str] = []
    release = threading.Event()
    worker_started = threading.Event()

    release_waits: list[bool] = []

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Block until released, standing in for an in-flight Rust transfer."""
        del reader_fd, writer_fd
        worker_started.set()
        release_waits.append(release.wait(timeout=5.0))
        events.append("worker_returned")
        return 0

    install_fake_pump(monkeypatch, blocking_pump)
    asyncio.run(
        cancel_mid_transfer(
            worker_started,
            release,
            guard=_RecordingGuard(events),
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
        f"restore must happen only after the worker thread returns, even "
        f"after {cancellations} cancellation(s); observed order {events}"
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

    release_waits: list[bool] = []

    def failing_pump(reader_fd: int, writer_fd: int) -> int:
        """Fail after the cancellation has been delivered."""
        del reader_fd, writer_fd
        worker_started.set()
        release_waits.append(release.wait(timeout=5.0))
        events.append("worker_returned")
        msg = "the pump failed while the hop was being cancelled"
        raise OSError(msg)

    install_fake_pump(monkeypatch, failing_pump)

    with (
        caplog.at_level(logging.ERROR, logger="asyncio"),
        caplog.at_level(logging.DEBUG, logger="cuprum._pipeline_streams"),
    ):
        # CancelledError, not OSError: cancel_mid_transfer asserts it.
        asyncio.run(
            cancel_mid_transfer(
                worker_started,
                release,
                guard=_RecordingGuard(events),
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
    unretrieved = [
        record.getMessage()
        for record in caplog.records
        if "never retrieved" in record.getMessage()
    ]
    assert not unretrieved, (
        "the pump's failure must be retrieved from the future, but asyncio "
        f"reported it as unhandled: {unretrieved}"
    )
    # Retrieving the failure is not enough: discarding it silently would leave
    # a genuine pump error with no trace at all, since the caller only ever
    # sees the cancellation.
    reported = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "rust_pump_failed_after_cancel"
    ]
    assert len(reported) == 1, (
        "a pump failure masked by cancellation must be recorded exactly once, "
        f"found {len(reported)}"
    )
    # The error travels as `exc_info` rather than in the message text, so the
    # record carries the traceback — without it the record would say a pump
    # failed but not where, which is the one thing it exists to answer.
    exc_info = reported[0].exc_info
    assert exc_info is not None, (
        "the record must attach the exception, so its traceback is preserved"
    )
    assert isinstance(exc_info[1], OSError), (
        f"the attached exception must be the pump's own, found {exc_info[1]!r}"
    )
    assert str(exc_info[1]) == "the pump failed while the hop was being cancelled", (
        f"the attached exception must carry the pump's message, found {exc_info[1]!r}"
    )
