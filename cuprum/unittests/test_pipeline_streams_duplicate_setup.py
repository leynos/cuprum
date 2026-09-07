"""Rust-pump writer-duplicate setup failure coverage."""

from __future__ import annotations

import asyncio
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
from cuprum.unittests._rust_pump_test_helpers import owned_fds


class _DuplicateSetupFailure:
    """Fault seam that fails either writer duplication or its blocking setup."""

    def __init__(
        self,
        *,
        duplicate_creation_fails: bool,
        fault_error: type[OSError] | type[ValueError],
    ) -> None:
        """Capture the failure stage and error type for one parametrized case."""
        self.duplicate_creation_fails = duplicate_creation_fails
        self.fault_error = fault_error
        self.duplicated_fds: list[int] = []
        self.resume_calls = 0
        self._original_dup = os.dup

    def pause_reader(
        self,
        reader: asyncio.StreamReader,
    ) -> _pipeline_stream_fds._ReaderPause:
        """Return a reader pause whose resume callback records rollback."""
        del reader
        return _pipeline_stream_fds._ReaderPause(resume=self.resume_reader)

    def resume_reader(self) -> None:
        """Record restoration of the paused asyncio reader."""
        self.resume_calls += 1

    async def drain_reader(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Skip reader draining while keeping the async seam faithful."""
        del reader, writer
        await asyncio.sleep(0)

    def duplicate(self, fd: int) -> int:
        """Create a duplicate or fail before a duplicate exists."""
        if self.duplicate_creation_fails:
            msg = "cannot duplicate writer descriptor"
            raise self.fault_error(msg)
        duplicate = self._original_dup(fd)
        self.duplicated_fds.append(duplicate)
        return duplicate

    def engage_blocking_mode(
        self,
        *,
        reader_fd: int,
        writer_fd: int,
    ) -> _pipeline_stream_fds._BlockingModeGuard:
        """Fail while configuring the duplicated descriptor state."""
        del reader_fd, writer_fd
        msg = "cannot configure duplicated writer descriptor"
        raise self.fault_error(msg)

    def assert_duplicate_cleanup(self) -> None:
        """Assert rollback closed only a duplicate that was actually created."""
        if self.duplicate_creation_fails:
            assert self.duplicated_fds == [], (
                "failed duplication creates no writer descriptor to clean up"
            )
            return
        for duplicate_fd in self.duplicated_fds:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(duplicate_fd)


def _assert_handoff_diagnostics(
    caplog: pytest.LogCaptureFixture,
    *,
    duplicate_creation_fails: bool,
    fault_error: type[OSError] | type[ValueError],
) -> None:
    """Assert diagnostics distinguish fatal duplication from a blocking decline."""
    handoff_records = [
        record.__dict__
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "rust_pump_handoff_failed"
    ]
    if duplicate_creation_fails:
        assert len(handoff_records) == 1, (
            "a failed duplicate setup must produce one hand-off diagnostic"
        )
        fields = handoff_records[0]
        assert fields["cuprum_phase"] == "duplicate_writer", (
            "the diagnostic must identify duplicate creation as the failed phase"
        )
        assert fields["cuprum_outcome"] == "failed", (
            "a failed duplicate setup must be recorded as failed"
        )
        assert fields["cuprum_error_type"] == fault_error.__name__, (
            "the diagnostic must preserve the duplicate failure category"
        )
        assert fields["cuprum_errno"] is None, (
            "a duplicate failure without errno must not invent one"
        )
        return
    assert handoff_records == [], "a declined blocking setup is not a fatal hand-off"


@pytest.mark.parametrize(
    ("duplicate_creation_fails", "fault_error"),
    [(True, OSError), (True, ValueError), (False, OSError), (False, ValueError)],
    ids=["dup-oserror", "dup-valueerror", "blocking-oserror", "blocking-valueerror"],
)
def test_rust_pump_rolls_back_duplicate_setup_failures(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    duplicate_creation_fails: bool,
    fault_error: type[OSError] | type[ValueError],
) -> None:
    """Duplicate failures propagate; blocking failures select the Python fallback."""
    caplog.set_level(logging.DEBUG, logger="cuprum._pipeline_streams")
    failure = _DuplicateSetupFailure(
        duplicate_creation_fails=duplicate_creation_fails,
        fault_error=fault_error,
    )
    monkeypatch.setattr(
        _pipeline_streams,
        "_pause_reader_transport",
        failure.pause_reader,
    )
    monkeypatch.setattr(
        _pipeline_streams,
        "_drain_reader_buffer",
        failure.drain_reader,
    )
    monkeypatch.setattr(_pipeline_stream_native_cleanup.os, "dup", failure.duplicate)
    if not duplicate_creation_fails:
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup._BlockingModeGuard,
            "engage",
            failure.engage_blocking_mode,
        )

    reader = typ.cast("asyncio.StreamReader", object())
    with owned_fds() as (reader_fd, writer_fd):
        handoff = _pipeline_stream_native_cleanup._RustPumpHandoff(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            cleanup_grace_s=0.5,
        )
        if duplicate_creation_fails:
            with pytest.raises(fault_error):
                asyncio.run(
                    _pipeline_streams._run_rust_pump(
                        reader=reader,
                        writer=None,
                        handoff=handoff,
                    )
                )
        else:
            handled = asyncio.run(
                _pipeline_streams._run_rust_pump(
                    reader=reader,
                    writer=None,
                    handoff=handoff,
                )
            )

            assert handled is False, "blocking failure must select Python fallback"
        assert failure.resume_calls == 1, "rollback must resume the reader"
        os.fstat(reader_fd)
        os.fstat(writer_fd)
        failure.assert_duplicate_cleanup()
    _assert_handoff_diagnostics(
        caplog,
        duplicate_creation_fails=duplicate_creation_fails,
        fault_error=fault_error,
    )


def test_reader_duplication_failure_retains_its_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader-duplication failure leaves no callback-owned descriptor open."""
    error = OSError("reader cannot be duplicated")

    def fail_reader_duplication(_fd: int) -> typ.NoReturn:
        """Fail before a reader duplicate exists."""
        raise error

    monkeypatch.setattr(
        _pipeline_stream_native_cleanup.os, "dup", fail_reader_duplication
    )
    with owned_fds() as (reader_fd, writer_fd):
        handoff = _pipeline_stream_native_cleanup._RustPumpHandoff(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            cleanup_grace_s=0.5,
        )
        with pytest.raises(
            _pipeline_stream_native_cleanup._RustPumpStateDuplicationError
        ) as exc_info:
            _pipeline_stream_native_cleanup._duplicate_rust_pump_state_fds(handoff)

    assert exc_info.value.error is error, "the wrapper must retain the reader error"


def test_writer_duplication_failure_closes_the_reader_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer-duplication failure rolls back the already-created reader FD."""
    original_dup = os.dup
    duplicated_readers: list[int] = []
    error = OSError("writer cannot be duplicated")

    def duplicate_reader_then_fail_writer(fd: int) -> int:
        """Create the first duplicate and fail the second call."""
        if duplicated_readers:
            raise error
        duplicate = original_dup(fd)
        duplicated_readers.append(duplicate)
        return duplicate

    monkeypatch.setattr(
        _pipeline_stream_native_cleanup.os,
        "dup",
        duplicate_reader_then_fail_writer,
    )
    with owned_fds() as (reader_fd, writer_fd):
        handoff = _pipeline_stream_native_cleanup._RustPumpHandoff(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            cleanup_grace_s=0.5,
        )
        with pytest.raises(
            _pipeline_stream_native_cleanup._RustPumpStateDuplicationError
        ) as exc_info:
            _pipeline_stream_native_cleanup._duplicate_rust_pump_state_fds(handoff)

    assert exc_info.value.error is error, "the wrapper must retain the writer error"
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(duplicated_readers[0])


@pytest.mark.parametrize("error", [OSError("unavailable"), ValueError("closed")])
def test_blocking_mode_failure_closes_both_state_duplicates(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError | ValueError,
) -> None:
    """Expected blocking failures wrap their original error after both closes."""
    close_reader = mock.Mock()
    close_writer = mock.Mock()

    def fail_engage(**_kwargs: object) -> typ.NoReturn:
        """Raise the parametrized expected blocking failure."""
        raise error

    monkeypatch.setattr(
        _pipeline_stream_native_cleanup._BlockingModeGuard,
        "engage",
        fail_engage,
    )
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_close_rust_reader_fd",
        close_reader,
    )
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_close_rust_writer_fd",
        close_writer,
    )

    with pytest.raises(
        _pipeline_stream_native_cleanup._RustPumpBlockingModeError
    ) as exc_info:
        _pipeline_stream_native_cleanup._engage_rust_pump_blocking_mode(11, 12)

    assert exc_info.value.error is error, "the wrapper must retain the blocking error"
    close_reader.assert_called_once_with(11)
    close_writer.assert_called_once_with(12)


def test_unexpected_blocking_mode_failure_closes_both_state_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected blocking failures preserve their type after both closes."""
    close_reader = mock.Mock()
    close_writer = mock.Mock()

    class _UnexpectedBlockingFailure(BaseException):
        """Sentinel exception outside the expected blocking failure contract."""

    def fail_engage(**_kwargs: object) -> typ.NoReturn:
        """Raise the unexpected failure without changing its identity."""
        raise _UnexpectedBlockingFailure

    monkeypatch.setattr(
        _pipeline_stream_native_cleanup._BlockingModeGuard,
        "engage",
        fail_engage,
    )
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_close_rust_reader_fd",
        close_reader,
    )
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_close_rust_writer_fd",
        close_writer,
    )

    with pytest.raises(_UnexpectedBlockingFailure):
        _pipeline_stream_native_cleanup._engage_rust_pump_blocking_mode(11, 12)

    close_reader.assert_called_once_with(11)
    close_writer.assert_called_once_with(12)
