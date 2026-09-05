"""Production-path events for Rust writer-resource hand-off boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import _pipeline_streams
from cuprum.adapters.pump_metrics import RUST_PUMP_HANDOFF_TOTAL, PumpMetricsHook
from cuprum.pump_events import PumpEvent, RustPumpHandoffOutcome
from cuprum.pump_observation import observe_pump
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _NoopGuard:
    """Blocking-mode guard double for direct submission-path tests."""

    def restore(self) -> None:
        """Restore nothing after a deliberately isolated submission attempt."""


class _BlockingSetupError(OSError):
    """Signal a duplicate that cannot be switched to blocking mode."""

    def __init__(self) -> None:
        """Initialize the test double's stable diagnostic message."""
        super().__init__("blocking mode is unavailable")


class _DuplicateWriterError(OSError):
    """Signal a writer descriptor that cannot be duplicated."""

    def __init__(self) -> None:
        """Initialize the test double's stable diagnostic message."""
        super().__init__("writer descriptor cannot be duplicated")


class _ExecutorRejectedError(OSError):
    """Signal an executor that rejects native pump submission."""

    def __init__(self) -> None:
        """Initialize the test double's stable diagnostic message."""
        super().__init__("executor is unavailable")


@contextlib.contextmanager
def _pipe_fds() -> cabc.Iterator[tuple[int, int]]:
    """Yield a pipe pair and release any descriptor neither worker owns."""
    reader_fd, writer_fd = os.pipe()
    try:
        yield reader_fd, writer_fd
    finally:
        for fd in (reader_fd, writer_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def _state(reader_fd: int, writer_fd: int) -> _pipeline_streams._RustPumpState:
    """Build state with no transport callback outside this hand-off seam."""
    return _pipeline_streams._RustPumpState(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
        blocking_mode_guard=typ.cast(
            "_pipeline_streams._BlockingModeGuard", _NoopGuard()
        ),
        resume_reader=None,
    )


def _handoff_outcomes(events: list[PumpEvent]) -> list[RustPumpHandoffOutcome]:
    """Return the closed outcomes emitted by one submission attempt."""
    return [
        typ.cast("RustPumpHandoffOutcome", event.outcome)
        for event in events
        if event.phase == "handoff"
    ]


def test_blocking_setup_failure_emits_one_handoff_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate setup decline records its bounded failure outcome once."""
    events: list[PumpEvent] = []

    def reject_duplicate_blocking_mode(fd: int, is_blocking: bool) -> None:
        """Reject only the duplicate that this test asks the pump to configure."""
        del fd, is_blocking
        raise _BlockingSetupError

    monkeypatch.setattr(
        _pipeline_streams.os,
        "set_blocking",
        reject_duplicate_blocking_mode,
    )
    with _pipe_fds() as (reader_fd, writer_fd), observe_pump(events.append):
        result = asyncio.run(
            _pipeline_streams._run_rust_pump_with_blocking_fds(
                state=_state(reader_fd, writer_fd),
            )
        )
        os.fstat(writer_fd)

    assert result is False, "blocking setup failure must decline the Rust pump"
    assert _handoff_outcomes(events) == [
        RustPumpHandoffOutcome.BLOCKING_SETUP_FAILED
    ], "a blocking decline must emit exactly its matching hand-off outcome"


def test_duplicate_writer_failure_emits_one_bounded_outcome(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed duplicate records its outcome without transferring ownership."""
    caplog.set_level(logging.DEBUG, logger="cuprum._pipeline_streams")
    events: list[PumpEvent] = []
    collector = RecordingCollector()
    restore = mock.Mock()
    resume_reader = mock.Mock()

    def fail_duplicate(writer_fd: int) -> typ.NoReturn:
        """Fail before a duplicate writer resource exists."""
        del writer_fd
        raise _DuplicateWriterError

    monkeypatch.setattr(_pipeline_streams.os, "dup", fail_duplicate)
    with (
        _pipe_fds() as (reader_fd, writer_fd),
        observe_pump(events.append),
        observe_pump(PumpMetricsHook(collector)),
    ):
        state = _pipeline_streams._RustPumpState(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            blocking_mode_guard=typ.cast(
                "_pipeline_streams._BlockingModeGuard",
                restore,
            ),
            resume_reader=resume_reader,
        )
        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(_DuplicateWriterError, match="cannot be duplicated"):
                _pipeline_streams._submit_rust_pump(loop=loop, state=state)
        finally:
            loop.close()
        os.fstat(writer_fd)

    restore.restore.assert_called_once_with()
    resume_reader.assert_called_once_with()
    assert _handoff_outcomes(events) == [
        RustPumpHandoffOutcome.DUPLICATE_WRITER_FAILED
    ], "duplicate failure must emit exactly its matching hand-off outcome"
    assert collector.counters == [
        (
            RUST_PUMP_HANDOFF_TOTAL,
            1.0,
            {"outcome": RustPumpHandoffOutcome.DUPLICATE_WRITER_FAILED},
        )
    ], "duplicate failure must increment one bounded hand-off metric"
    records = [
        record.__dict__
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "rust_pump_handoff_failed"
    ]
    assert len(records) == 1, "duplicate failure must produce one hand-off record"
    assert records[0]["cuprum_phase"] == "duplicate_writer", (
        "duplicate failure must keep the existing bounded diagnostic phase"
    )
    assert records[0]["cuprum_error_type"] == "_DuplicateWriterError", (
        "duplicate diagnostic must retain the error category"
    )


def test_executor_rejection_emits_no_submitted_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected submission records only the rejection outcome."""
    events: list[PumpEvent] = []

    async def reject_submission(
        state: _pipeline_streams._RustPumpState,
    ) -> None:
        """Reject the executor call before it accepts the duplicate."""
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()

        def reject(
            executor: object,
            function: object,
            *args: object,
        ) -> typ.NoReturn:
            """Model an executor that cannot accept this work item."""
            del executor, function, args
            raise _ExecutorRejectedError

        with mock.patch.object(loop, "run_in_executor", side_effect=reject):
            _pipeline_streams._submit_rust_pump(loop=loop, state=state)

    with _pipe_fds() as (reader_fd, writer_fd), observe_pump(events.append):
        with pytest.raises(OSError, match="executor is unavailable"):
            asyncio.run(reject_submission(_state(reader_fd, writer_fd)))
        os.fstat(writer_fd)

    assert _handoff_outcomes(events) == [
        RustPumpHandoffOutcome.EXECUTOR_SUBMISSION_REJECTED
    ], "rejection must emit no submitted outcome"


def test_accepted_submission_emits_submitted_after_the_worker_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successfully accepted work item emits one submitted outcome."""
    events: list[PumpEvent] = []

    def close_received_duplicate(reader_fd: int, writer_fd: int) -> int:
        """Model Rust consuming the duplicate after executor acceptance."""
        del reader_fd
        os.close(writer_fd)
        return 0

    async def accept_submission(state: _pipeline_streams._RustPumpState) -> bool:
        """Run the submitted callable inline while preserving its copied context."""
        loop = asyncio.get_running_loop()

        def accept(
            executor: object,
            function: cabc.Callable[..., object],
            *args: object,
        ) -> asyncio.Future[int]:
            """Return a future only after the worker callable has accepted work."""
            del executor
            future = loop.create_future()
            future.set_result(typ.cast("int", function(*args)))
            return future

        with mock.patch.object(loop, "run_in_executor", side_effect=accept):
            future = _pipeline_streams._submit_rust_pump(loop=loop, state=state)
            assert future is not None, "accepted submission must return its future"
            await future
        return True

    import cuprum._streams_rs as streams_rs

    monkeypatch.setattr(streams_rs, "rust_pump_stream", close_received_duplicate)
    with _pipe_fds() as (reader_fd, writer_fd), observe_pump(events.append):
        assert asyncio.run(accept_submission(_state(reader_fd, writer_fd))) is True
        os.fstat(writer_fd)

    assert _handoff_outcomes(events) == [RustPumpHandoffOutcome.SUBMITTED], (
        "accepted submission must emit one submitted outcome after acceptance"
    )
