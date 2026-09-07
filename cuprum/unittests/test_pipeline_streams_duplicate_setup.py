"""Linux worker-descriptor setup failure coverage for native pipeline pumping."""

from __future__ import annotations

import asyncio
import os
import sys
import typing as typ

import pytest

from cuprum import _pipeline_stream_fds, _pipeline_streams
from cuprum.unittests._rust_pump_test_helpers import owned_fds

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux uses independently opened worker descriptor descriptions",
)


class _WorkerFdSetupFailure:
    """Fault seam for worker FD creation or blocking-mode preparation."""

    def __init__(
        self,
        *,
        writer_open_fails: bool,
        fault_error: type[OSError] | type[ValueError],
    ) -> None:
        """Capture one preparation failure mode and the worker FDs it created."""
        self.writer_open_fails = writer_open_fails
        self.fault_error = fault_error
        self.worker_fds: list[int] = []
        self.resume_calls = 0
        self._open = os.open

    def pause_reader(
        self,
        reader: asyncio.StreamReader,
    ) -> _pipeline_stream_fds._ReaderPause:
        """Return a pause whose resume callback records fallback cleanup."""
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
        """Skip reader draining while preserving the asynchronous hand-off seam."""
        del reader, writer
        await asyncio.sleep(0)

    def open(self, path: str, flags: int) -> int:
        """Create the reader worker then optionally reject the writer worker."""
        if self.writer_open_fails and self.worker_fds:
            msg = "cannot open writer worker descriptor"
            raise self.fault_error(msg)
        worker_fd = self._open(path, flags)
        self.worker_fds.append(worker_fd)
        return worker_fd

    def engage(self, **_kwargs: object) -> typ.NoReturn:
        """Reject blocking-mode preparation after both worker FDs exist."""
        msg = "cannot configure worker descriptors"
        raise self.fault_error(msg)

    def assert_worker_cleanup(self) -> None:
        """Assert every worker descriptor created before fallback was closed."""
        expected_count = 1 if self.writer_open_fails else 2
        assert len(self.worker_fds) == expected_count, (
            "worker preparation must create only the descriptors preceding its "
            f"failure, found {self.worker_fds}"
        )
        for worker_fd in self.worker_fds:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(worker_fd)


@pytest.mark.parametrize(
    ("writer_open_fails", "fault_error"),
    [(True, OSError), (True, ValueError), (False, OSError), (False, ValueError)],
    ids=["open-oserror", "open-valueerror", "blocking-oserror", "blocking-valueerror"],
)
def test_rust_pump_falls_back_after_worker_fd_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    writer_open_fails: bool,
    fault_error: type[OSError] | type[ValueError],
) -> None:
    """Unsafe worker preparation closes its FDs and restores Python ownership."""
    failure = _WorkerFdSetupFailure(
        writer_open_fails=writer_open_fails,
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
    monkeypatch.setattr(_pipeline_stream_fds.os, "open", failure.open)
    if not writer_open_fails:
        monkeypatch.setattr(
            _pipeline_stream_fds._BlockingModeGuard,
            "engage",
            failure.engage,
        )

    reader = typ.cast("asyncio.StreamReader", object())
    with owned_fds() as (reader_fd, writer_fd):
        handled = asyncio.run(
            _pipeline_streams._run_rust_pump(
                reader=reader,
                writer=None,
                reader_fd=reader_fd,
                writer_fd=writer_fd,
            )
        )

        assert handled is False, "unsafe worker setup must select Python fallback"
        assert failure.resume_calls == 1, "fallback must resume the reader once"
        os.fstat(reader_fd)
        os.fstat(writer_fd)
        failure.assert_worker_cleanup()
