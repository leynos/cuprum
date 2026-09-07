"""Production-path events for Rust worker-descriptor hand-off boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import _pipeline_stream_fds, _pipeline_streams
from cuprum.pump_events import PumpEvent, RustPumpDeclineReason, RustPumpHandoffOutcome
from cuprum.pump_observation import observe_pump

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _NoopGuard:
    """Blocking-mode guard double for direct submission-path tests."""

    def restore(self) -> None:
        """Restore nothing after a deliberately isolated submission attempt."""


class _ExecutorRejectedError(OSError):
    """Signal an executor that rejects native pump submission."""

    def __init__(self) -> None:
        """Initialize the test double's stable diagnostic message."""
        super().__init__("executor is unavailable")


@contextlib.contextmanager
def _pipe_fds() -> cabc.Iterator[tuple[int, int]]:
    """Yield a pipe pair and close whichever descriptors remain Python-owned."""
    reader_fd, writer_fd = os.pipe()
    try:
        yield reader_fd, writer_fd
    finally:
        for fd in (reader_fd, writer_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def _state(reader_fd: int, writer_fd: int) -> _pipeline_streams._RustPumpState:
    """Build worker-owned state with no transport callback."""
    worker_fds = _pipeline_stream_fds._open_native_pump_worker_fds(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
    )
    assert worker_fds is not None, "Linux tests require worker FD preparation"
    return _pipeline_streams._RustPumpState(
        reader_fd=worker_fds.reader_fd,
        writer_fd=worker_fds.writer_fd,
        blocking_mode_guard=typ.cast(
            "_pipeline_streams._BlockingModeGuard", _NoopGuard()
        ),
        resume_reader=None,
        close_reader_fd=True,
        close_writer_fd=True,
    )


def _handoff_outcomes(events: list[PumpEvent]) -> list[RustPumpHandoffOutcome]:
    """Return the closed outcomes emitted by one submission attempt."""
    return [
        typ.cast("RustPumpHandoffOutcome", event.outcome)
        for event in events
        if event.phase == "handoff"
    ]


def test_blocking_setup_failure_reports_a_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker blocking failure declines before the Rust ownership boundary."""
    events: list[PumpEvent] = []

    def reject_worker_blocking_mode(**_kwargs: object) -> typ.NoReturn:
        """Reject preparation before any worker descriptor reaches Rust."""
        msg = "blocking mode is unavailable"
        raise OSError(msg)

    monkeypatch.setattr(
        _pipeline_stream_fds._BlockingModeGuard,
        "engage",
        reject_worker_blocking_mode,
    )
    with _pipe_fds() as (reader_fd, writer_fd), observe_pump(events.append):
        result = asyncio.run(
            _pipeline_streams._pump_over_raw_fds(
                reader=typ.cast("asyncio.StreamReader", object()),
                writer=None,
                reader_fd=reader_fd,
                writer_fd=writer_fd,
            )
        )

    assert result is False, "blocking setup failure must decline the Rust pump"
    assert [(event.phase, event.reason) for event in events] == [
        ("declined", RustPumpDeclineReason.BLOCKING_MODE_UNAVAILABLE)
    ], "a preparation decline must retain its bounded routing reason"


def test_executor_rejection_emits_no_submitted_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected submission records only the rejection outcome."""
    events: list[PumpEvent] = []

    async def reject_submission(state: _pipeline_streams._RustPumpState) -> None:
        """Reject the executor call before it accepts worker-owned resources."""
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
        os.fstat(reader_fd)
        os.fstat(writer_fd)

    assert _handoff_outcomes(events) == [
        RustPumpHandoffOutcome.EXECUTOR_SUBMISSION_REJECTED
    ], "rejection must emit no submitted outcome"


def test_accepted_submission_emits_submitted_after_the_worker_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successfully accepted work item emits one submitted outcome."""
    events: list[PumpEvent] = []

    def close_received_writer(reader_fd: int, writer_fd: int) -> int:
        """Model Rust consuming its worker writer after executor acceptance."""
        del reader_fd
        os.close(writer_fd)
        return 0

    async def accept_submission(state: _pipeline_streams._RustPumpState) -> bool:
        """Run the submitted callable inline while preserving copied context."""
        loop = asyncio.get_running_loop()

        def accept(
            executor: object,
            function: cabc.Callable[..., object],
            *args: object,
        ) -> asyncio.Future[int]:
            """Return a future after the worker callable accepts its resources."""
            del executor
            future = loop.create_future()
            future.set_result(typ.cast("int", function(*args)))
            return future

        with mock.patch.object(loop, "run_in_executor", side_effect=accept):
            future = _pipeline_streams._submit_rust_pump(loop=loop, state=state)
            assert future is not None, "accepted submission must return its future"
            await future
        _pipeline_streams._restore_rust_pump_state(state)
        return True

    import cuprum._streams_rs as streams_rs

    monkeypatch.setattr(streams_rs, "rust_pump_stream", close_received_writer)
    with _pipe_fds() as (reader_fd, writer_fd), observe_pump(events.append):
        assert asyncio.run(accept_submission(_state(reader_fd, writer_fd))) is True
        os.fstat(reader_fd)
        os.fstat(writer_fd)

    assert _handoff_outcomes(events) == [RustPumpHandoffOutcome.SUBMITTED], (
        "accepted submission must emit one submitted outcome after acceptance"
    )
