"""Rust-pump writer-duplicate setup failure coverage."""

from __future__ import annotations

import asyncio
import logging
import os
import typing as typ

import pytest

from cuprum import _pipeline_stream_fds, _pipeline_streams
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
        self._original_set_blocking = os.set_blocking

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

    def set_blocking(self, fd: int, is_blocking: bool) -> None:
        """Fail only while configuring the created duplicate."""
        if self.duplicated_fds and fd == self.duplicated_fds[0]:
            msg = "cannot configure duplicated writer descriptor"
            raise self.fault_error(msg)
        self._original_set_blocking(fd, is_blocking)

    def assert_duplicate_cleanup(self) -> None:
        """Assert rollback closed only a duplicate that was actually created."""
        if self.duplicate_creation_fails:
            assert self.duplicated_fds == [], (
                "failed duplication creates no writer descriptor to clean up"
            )
            return
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(self.duplicated_fds[0])


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
    monkeypatch.setattr(_pipeline_streams.os, "dup", failure.duplicate)
    monkeypatch.setattr(_pipeline_streams.os, "set_blocking", failure.set_blocking)

    reader = typ.cast("asyncio.StreamReader", object())
    with owned_fds() as (reader_fd, writer_fd):
        if duplicate_creation_fails:
            with pytest.raises(fault_error):
                asyncio.run(
                    _pipeline_streams._run_rust_pump(
                        reader=reader,
                        writer=None,
                        reader_fd=reader_fd,
                        writer_fd=writer_fd,
                    )
                )
        else:
            handled = asyncio.run(
                _pipeline_streams._run_rust_pump(
                    reader=reader,
                    writer=None,
                    reader_fd=reader_fd,
                    writer_fd=writer_fd,
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
