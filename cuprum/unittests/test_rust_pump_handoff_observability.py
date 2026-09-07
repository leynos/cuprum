"""Production-path events for Rust writer-resource hand-off boundaries."""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import contextlib
import logging
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import (
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
)
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


class _InlineNativePumpExecutor:
    """Submit a native-pump double synchronously with concurrent-Future semantics."""

    def submit(
        self,
        function: cabc.Callable[..., int],
        *args: object,
    ) -> cf.Future[int]:
        """Run the worker now and return its already-settled future."""
        future: cf.Future[int] = cf.Future()
        try:
            future.set_result(function(*args))
        except OSError as error:
            future.set_exception(error)
        return future


class _RejectingNativePumpExecutor:
    """Refuse every native-pump work submission."""

    def submit(self, _function: object, *_args: object) -> typ.NoReturn:
        """Raise the stable rejection used by the hand-off test."""
        raise _ExecutorRejectedError


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


def _state(
    reader_fd: int,
    writer_fd: int,
) -> _pipeline_stream_native_cleanup._RustPumpState:
    """Build state with no transport callback outside this hand-off seam."""
    return _pipeline_stream_native_cleanup._RustPumpState(
        reader_fd=os.dup(reader_fd),
        writer_fd=os.dup(writer_fd),
        blocking_mode_guard=typ.cast(
            "_pipeline_stream_fds._BlockingModeGuard", _NoopGuard()
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


def test_blocking_setup_failure_emits_its_bounded_handoff_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-handoff blocking refusal records its bounded failure outcome."""
    events: list[PumpEvent] = []

    def reject_duplicate_blocking_mode(**_kwargs: object) -> typ.NoReturn:
        """Reject descriptor state setup before native ownership begins."""
        raise _BlockingSetupError

    monkeypatch.setattr(
        _pipeline_stream_fds._BlockingModeGuard,
        "engage",
        reject_duplicate_blocking_mode,
    )
    with _pipe_fds() as (reader_fd, writer_fd), observe_pump(events.append):
        result = asyncio.run(
            _pipeline_streams._pump_over_raw_fds(
                reader=typ.cast("asyncio.StreamReader", object()),
                writer=None,
                handoff=_pipeline_stream_native_cleanup._RustPumpHandoff(
                    reader_fd=reader_fd,
                    writer_fd=writer_fd,
                    cleanup_grace_s=0.5,
                ),
            )
        )
        os.fstat(writer_fd)

    assert result is False, "blocking setup failure must decline the Rust pump"
    assert _handoff_outcomes(events) == [
        RustPumpHandoffOutcome.BLOCKING_SETUP_FAILED
    ], "blocking refusal must retain its documented bounded hand-off outcome"


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

    with (
        _pipe_fds() as (reader_fd, writer_fd),
        observe_pump(events.append),
        observe_pump(PumpMetricsHook(collector)),
    ):
        state = _state(reader_fd, writer_fd)
        state.blocking_mode_guard = typ.cast(
            "_pipeline_stream_fds._BlockingModeGuard",
            restore,
        )
        state.resume_reader = resume_reader
        monkeypatch.setattr(_pipeline_stream_native_cleanup.os, "dup", fail_duplicate)

        async def start_with_duplicate_failure() -> None:
            """Start the hand-off from a running event loop."""
            await asyncio.sleep(0)
            with pytest.raises(_DuplicateWriterError, match="cannot be duplicated"):
                _pipeline_stream_native_cleanup._start_rust_pump_with_cleanup(state)

        asyncio.run(start_with_duplicate_failure())
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
        state: _pipeline_stream_native_cleanup._RustPumpState,
    ) -> None:
        """Reject the executor call before it accepts the duplicate."""
        await asyncio.sleep(0)
        with mock.patch.object(
            _pipeline_stream_native_cleanup,
            "_NATIVE_PUMP_EXECUTOR",
            _RejectingNativePumpExecutor(),
        ):
            _pipeline_stream_native_cleanup._start_rust_pump_with_cleanup(state)

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

    async def accept_submission(
        state: _pipeline_stream_native_cleanup._RustPumpState,
    ) -> bool:
        """Run the submitted callable inline while preserving its copied context."""
        with mock.patch.object(
            _pipeline_stream_native_cleanup,
            "_NATIVE_PUMP_EXECUTOR",
            _InlineNativePumpExecutor(),
        ):
            future, cleanup_complete = (
                _pipeline_stream_native_cleanup._start_rust_pump_with_cleanup(state)
            )
            await asyncio.wrap_future(future)
            await cleanup_complete
        return True

    import cuprum._streams_rs as streams_rs

    monkeypatch.setattr(streams_rs, "rust_pump_stream", close_received_duplicate)
    with _pipe_fds() as (reader_fd, writer_fd), observe_pump(events.append):
        assert asyncio.run(accept_submission(_state(reader_fd, writer_fd))) is True
        os.fstat(writer_fd)

    assert _handoff_outcomes(events) == [RustPumpHandoffOutcome.SUBMITTED], (
        "accepted submission must emit one submitted outcome after acceptance"
    )
