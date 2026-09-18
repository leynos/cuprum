"""Fault-injection tests for the Rust pump's reader-pause lifecycle.

Before `cuprum._pipeline_streams` hands the raw pipe descriptors to the Rust
pump it pauses the reader transport and ensures every successful pause is
resumed. These tests inject faults into that production seam to pin the
partial-failure behaviour #74 calls out — no missing resume, correct fallback,
and no swallowed unexpected pipe error. The blocking-mode seam is exercised
separately in `test_pipeline_streams_blocking_mode`.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import _pipeline_stream_fds, _pipeline_streams
from cuprum.pump_events import RustPumpDeclineReason
from cuprum.unittests._rust_pump_test_helpers import owned_fds


@pytest.mark.parametrize("fault_error", [OSError, ValueError])
def test_run_rust_pump_falls_back_and_resumes_when_blocking_fails(
    monkeypatch: pytest.MonkeyPatch,
    fault_error: type[OSError] | type[ValueError],
) -> None:
    """A blocking-toggle failure returns the Python-fallback signal and resumes.

    Both halves of ``_pump_over_raw_fds``' catch are driven: ``os.set_blocking``
    reports a closed descriptor as ``ValueError`` and a bad one as ``OSError``,
    so a fallback that only survived the latter would still crash a hop the
    Python pump could have carried.
    """
    resume_calls = {"count": 0}

    def raise_blocking_error(**_kwargs: object) -> tuple[bool, bool]:
        """Fail the blocking-mode hand-off as the chosen fault class."""
        msg = "cannot switch descriptor to blocking mode"
        raise fault_error(msg)

    def fake_pause_reader_transport(
        reader: asyncio.StreamReader,
    ) -> _pipeline_stream_fds._ReaderPause:
        """Return a resume callback that records how often it is invoked."""
        del reader

        def _resume() -> None:
            """Record a resume invocation."""
            resume_calls["count"] += 1

        return _pipeline_stream_fds._ReaderPause(resume=_resume)

    async def fake_drain_reader_buffer(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Skip the real buffer flush."""
        del reader, writer
        await asyncio.sleep(0)

    monkeypatch.setattr(
        _pipeline_streams,
        "_pause_reader_transport",
        fake_pause_reader_transport,
    )
    monkeypatch.setattr(
        _pipeline_streams,
        "_drain_reader_buffer",
        fake_drain_reader_buffer,
    )
    monkeypatch.setattr(
        _pipeline_stream_fds,
        "_set_stream_fds_blocking",
        raise_blocking_error,
    )

    reader = typ.cast("asyncio.StreamReader", object())
    with owned_fds() as (reader_fd, writer_fd):
        handled = asyncio.run(
            _pipeline_streams._run_rust_pump(
                reader=reader,
                writer=None,
                handoff=_pipeline_streams._RustPumpHandoff(
                    reader_fd=reader_fd,
                    writer_fd=writer_fd,
                    cleanup_grace_s=0.5,
                ),
            )
        )

    assert handled is False, "a blocking-toggle failure must fall back to Python"
    assert resume_calls["count"] == 1, "the reader must be resumed on fallback"


def test_pump_over_raw_fds_falls_back_when_pause_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pause falls back to Python without touching the descriptors."""
    engaged = {"count": 0}

    def fake_engage(**_kwargs: object) -> object:
        """Fail the test if the descriptors are switched after a failed pause."""
        engaged["count"] += 1
        msg = "blocking mode must not be engaged after a failed pause"
        raise AssertionError(msg)

    monkeypatch.setattr(
        _pipeline_streams,
        "_pause_reader_transport",
        lambda _reader: _pipeline_stream_fds._ReaderPause(
            decline_reason=RustPumpDeclineReason.READER_PAUSE_FAILED,
        ),
    )
    monkeypatch.setattr(_pipeline_stream_fds._BlockingModeGuard, "engage", fake_engage)

    reader = typ.cast("asyncio.StreamReader", object())
    with owned_fds() as (reader_fd, writer_fd):
        handled = asyncio.run(
            _pipeline_streams._pump_over_raw_fds(
                reader=reader,
                writer=None,
                handoff=_pipeline_streams._RustPumpHandoff(
                    reader_fd=reader_fd,
                    writer_fd=writer_fd,
                    cleanup_grace_s=0.5,
                ),
            )
        )

    assert handled is False, "a failed pause must report the Python-fallback signal"
    assert engaged["count"] == 0, (
        "a failed pause must return before engaging blocking mode"
    )
