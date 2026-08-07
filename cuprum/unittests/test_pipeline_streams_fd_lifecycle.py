"""Fault-injection tests for the Rust pump's reader-pause lifecycle.

Before `cuprum._pipeline_streams` hands the raw pipe descriptors to the Rust
pump it pauses the reader transport through `_paused_reader`, a context manager
that must always resume a pause it completed. These tests inject faults into
that seam to pin the partial-failure behaviour #74 calls out — no missing
resume, correct fallback, and no swallowed unexpected pipe error. The
blocking-mode seam is exercised separately in
`test_pipeline_streams_blocking_mode`.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum import _pipeline_stream_fds, _pipeline_streams
from cuprum._pipeline_stream_fds import _paused_reader
from cuprum.pump_events import RustPumpDeclineReason
from cuprum.unittests._rust_pump_test_helpers import (
    PauseOnlyTransport,
    TransportOnlyReader,
    owned_fds,
)


class _FakeTransport:
    """Reader transport recording pause/resume calls for the CM tests."""

    def __init__(self, *, pause_raises: bool = False) -> None:
        """Start with no calls recorded."""
        self.pause_calls = 0
        self.resume_calls = 0
        self._pause_raises = pause_raises

    def pause_reading(self) -> None:
        """Record a pause, optionally failing as a torn-down transport would."""
        self.pause_calls += 1
        if self._pause_raises:
            msg = "cannot pause a closing transport"
            raise RuntimeError(msg)

    def resume_reading(self) -> None:
        """Record a resume."""
        self.resume_calls += 1
def _raise_boom() -> None:
    """Raise a deterministic error from inside a paused-reader block."""
    msg = "boom"
    raise ValueError(msg)


@given(body_raises=st.booleans())
def test_paused_reader_always_resumes_a_pausable_transport(
    *,
    body_raises: bool,
) -> None:
    """Resume fires exactly once on both the normal and exception exits."""
    transport = _FakeTransport()
    reader = typ.cast("asyncio.StreamReader", TransportOnlyReader(transport))

    if body_raises:
        with pytest.raises(ValueError, match="boom"), _paused_reader(reader):
            _raise_boom()
    else:
        with _paused_reader(reader):
            pass

    assert transport.pause_calls == 1, "the transport must be paused exactly once"
    assert transport.resume_calls == 1, (
        "a completed pause must be resumed exactly once, on every exit path"
    )

async def _hold_the_pause_until_cancelled(
    reader: asyncio.StreamReader,
    entered: asyncio.Event,
) -> None:
    """Keep a completed pause open across an await, until cancelled."""
    with _paused_reader(reader):
        entered.set()
        await asyncio.Event().wait()

async def _cancel_a_held_pause(reader: asyncio.StreamReader) -> None:
    """Cancel a task parked inside ``_paused_reader`` and re-raise its outcome."""
    entered = asyncio.Event()
    task = asyncio.create_task(_hold_the_pause_until_cancelled(reader, entered))
    # Waiting on `entered` rather than a bare sleep guarantees the pause is
    # applied and the task is parked mid-block; a cancellation delivered before
    # the block was entered would exercise nothing.
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

def test_paused_reader_resumes_when_the_block_is_cancelled() -> None:
    """A task cancelled inside the block still resumes, and stays cancelled.

    Cancellation is the exit path the synchronous cases cannot reach.
    ``CancelledError`` derives from ``BaseException``, so narrowing the resume
    to an ``except Exception`` guard would skip it — leaving the reader paused
    for the rest of the hop while every other case here still passed.
    """
    transport = _FakeTransport()
    reader = typ.cast("asyncio.StreamReader", TransportOnlyReader(transport))

    asyncio.run(_cancel_a_held_pause(reader))

    assert transport.pause_calls == 1, "the transport must be paused exactly once"
    assert transport.resume_calls == 1, (
        "a pause held across a cancelled await must still be resumed exactly once"
    )
class _ResumeOnlyTransport:
    """A transport offering ``resume_reading`` but no ``pause_reading``.

    Calling resume here would mean resuming a pause that never happened, so the
    method fails rather than recording, making the mistake observable instead of
    merely absent.
    """

    def resume_reading(self) -> None:
        """Fail: nothing was paused, so nothing may be resumed."""
        msg = "resume_reading must not run when no pause was applied"
        raise AssertionError(msg)


def test_paused_reader_skips_resume_when_pause_unavailable() -> None:
    """A transport with no pause hook hands off, and is never resumed.

    A bare ``object()`` would only prove the context manager does not crash. A
    transport that exposes ``resume_reading`` alone catches an implementation
    that resumes whatever hook it can find regardless of whether a pause
    succeeded.
    """
    reader = typ.cast(
        "asyncio.StreamReader", TransportOnlyReader(_ResumeOnlyTransport())
    )

    with _paused_reader(reader) as pause:
        assert pause.may_hand_off is True, (
            "with no callbacks to race, the descriptor hand-off is safe"
        )
def test_paused_reader_declines_an_unresumable_transport() -> None:
    """A pausable transport with no resume hook declines the hand-off, unpaused.

    The pause hook is evidence of read callbacks that would race the Rust pump,
    so handing the descriptor over is unsafe. Pausing is no remedy either: the
    Python fallback reads the same stream and would stall on a reader nothing
    can restart. The only safe move is to leave the transport untouched.
    """
    transport = PauseOnlyTransport()
    reader = typ.cast("asyncio.StreamReader", TransportOnlyReader(transport))

    with _paused_reader(reader) as pause:
        assert pause.decline_reason is RustPumpDeclineReason.READER_UNRESUMABLE, (
            "a transport that cannot be resumed must decline the hand-off, and "
            "say so in its own name rather than a pause that raised"
        )

    assert transport.pause_calls == 0, (
        "a pause that could never be undone must not be applied"
    )
def test_paused_reader_undoes_a_half_applied_pause() -> None:
    """A pause that raises is corrected once, at the failure site."""
    transport = _FakeTransport(pause_raises=True)
    reader = typ.cast("asyncio.StreamReader", TransportOnlyReader(transport))

    with _paused_reader(reader) as pause:
        assert pause.decline_reason is RustPumpDeclineReason.READER_PAUSE_FAILED, (
            "a pause that raised must report the hand-off as unsafe, named "
            "apart from a transport that was never paused at all"
        )

    assert transport.pause_calls == 1, "the pause must be attempted exactly once"
    # A transport can set its paused flag before whatever raised, and the
    # Python fallback then reads a descriptor nothing is watching. The resume
    # runs at the failure site, not at block exit, so exactly one fires.
    assert transport.resume_calls == 1, (
        "a pause that raised must be undone, in case it half-applied"
    )
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
                reader_fd=reader_fd,
                writer_fd=writer_fd,
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
                reader_fd=reader_fd,
                writer_fd=writer_fd,
            )
        )

    assert handled is False, "a failed pause must report the Python-fallback signal"
    assert engaged["count"] == 0, (
        "a failed pause must return before engaging blocking mode"
    )
