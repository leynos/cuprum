"""Native hand-off failure outcomes on the Rust pump fast path.

These cover what a caller of `_run_rust_pump` observes when descriptor
duplication fails: the decline that hands the hop back to the Python pump, and
the fatal close that releases the writer when no fallback remains. The
duplication-setup helpers each use are covered by
`test_pipeline_streams_duplicate_setup`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import typing as typ

import pytest

from cuprum import (
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
)
from cuprum.pump_events import (
    PumpEvent,
    RustPumpDeclineReason,
    RustPumpHandoffOutcome,
)
from cuprum.pump_observation import observe_pump
from cuprum.unittests._rust_pump_test_helpers import owned_fds


def test_worker_duplication_failure_declines_instead_of_raising(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed worker-descriptor re-open falls back rather than wedging the hop.

    This is the regression for the intermittent native-pump hang. Re-opening a
    transport's descriptor races asyncio's own close, so a short-lived upstream
    stage fails the re-open on a hop the Python pump could have carried. The
    old re-raise left the writer transport open, so the downstream stage never
    saw EOF and the pipeline hung until its deadline; the decline is what makes
    the hop complete.

    The last two assertions are the ones that matter. Resuming the reader is
    what returns the transport to asyncio before the fallback reads it, and
    `handle` being `False` is what routes the hop to `_pump_stream` at all — a
    re-raise would satisfy neither.
    """
    caplog.set_level(logging.DEBUG, logger="cuprum._pipeline_streams")
    resume_calls = 0
    events: list[PumpEvent] = []

    def resume_reader() -> None:
        """Record restoration of the paused asyncio reader."""
        nonlocal resume_calls
        resume_calls += 1

    monkeypatch.setattr(
        _pipeline_streams,
        "_pause_reader_transport",
        lambda _reader: _pipeline_stream_fds._ReaderPause(resume=resume_reader),
    )
    monkeypatch.setattr(
        _pipeline_streams,
        "_drain_reader_buffer",
        lambda _reader, _writer: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_open_native_pump_worker_fds",
        lambda **_kwargs: None,
    )

    reader = typ.cast("asyncio.StreamReader", object())
    with owned_fds() as (reader_fd, writer_fd), observe_pump(events.append):
        handle = asyncio.run(
            _pipeline_streams._run_rust_pump(
                reader=reader,
                writer=None,
                handoff=_pipeline_stream_native_cleanup._RustPumpHandoff(
                    reader_fd=reader_fd,
                    writer_fd=writer_fd,
                    cleanup_grace_s=0.5,
                ),
            )
        )
        os.fstat(reader_fd)
        os.fstat(writer_fd)

    assert handle is False, (
        "a failed worker-descriptor re-open must decline the fast path so the "
        f"Python pump carries the hop, found handled={handle!r}"
    )
    assert resume_calls == 1, "the decline must resume the paused reader"
    assert [event.phase for event in events] == ["declined", "handoff"], (
        f"the decline must report both its reason and its bounded outcome, "
        f"found {[event.phase for event in events]}"
    )
    assert events[0].reason == RustPumpDeclineReason.DUPLICATE_FDS_UNAVAILABLE, (
        "the decline must name the descriptor duplication seam"
    )
    assert events[1].outcome == RustPumpHandoffOutcome.DUPLICATE_WRITER_FAILED, (
        "the hand-off outcome must still name the failed duplication"
    )


def test_fatal_handoff_failure_closes_the_writer_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-raising hand-off releases the writer so the failure surfaces.

    The decline path returns `False` and keeps the writer, because the Python
    pump still has to write through it. A failure with no fallback is the
    opposite case: with nothing left to carry the hop, an open writer leaves
    the downstream stage waiting for an EOF that never comes, and the pipeline
    reports its deadline instead of the error that caused it. Closing the
    writer lets that stage exit, so the caller sees the real failure — the same
    wedge the decline fix removed, on the paths that must still raise.
    """
    closed_writers: list[object] = []

    async def record_close(writer: object) -> None:
        """Record the writer the fatal path released."""
        # The real close is a coroutine, so this fake must be awaitable and
        # must not block; the yield keeps it a genuine coroutine.
        await asyncio.sleep(0)
        closed_writers.append(writer)

    def fail_duplication(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Fail the second duplication stage, which has no fallback."""
        msg = "cannot duplicate owned descriptors"
        raise OSError(msg)

    monkeypatch.setattr(_pipeline_streams, "_close_stream_writer", record_close)
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_duplicate_native_pump_fds",
        fail_duplication,
    )

    writer = typ.cast("asyncio.StreamWriter", object())
    reader = typ.cast("asyncio.StreamReader", object())
    with owned_fds() as (reader_fd, writer_fd):
        with pytest.raises(OSError, match="cannot duplicate owned descriptors"):
            asyncio.run(
                _pipeline_streams._run_rust_pump(
                    reader=reader,
                    writer=writer,
                    handoff=_pipeline_stream_native_cleanup._RustPumpHandoff(
                        reader_fd=reader_fd,
                        writer_fd=writer_fd,
                        cleanup_grace_s=0.5,
                    ),
                )
            )
        os.fstat(reader_fd)
        os.fstat(writer_fd)

    assert closed_writers == [writer], (
        "a fatal hand-off failure must close the writer transport it cannot "
        f"fall back through, closed {closed_writers!r}"
    )
