"""Fault-injection tests for the Rust pump's FD pause/blocking lifecycle.

`cuprum._pipeline_streams` hands the raw pipe descriptors to the Rust pump under
two seams: `_BlockingModeGuard`, which switches the descriptors to blocking mode
and restores their prior mode, and `_paused_reader`, a context manager that
pauses the reader transport and always resumes it. These tests inject faults
into both to pin the partial-failure behaviour #74 calls out — no leaked
blocking state, no missing resume, correct fallback, and no swallowed
unexpected pipe error.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import typing as typ
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum import _pipeline_stream_fds, _pipeline_streams
from cuprum._pipeline_stream_fds import _BlockingModeGuard, _paused_reader

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_unix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.set_blocking on pipe FDs is a Unix contract",
)


@contextlib.contextmanager
def _pipe_fds() -> cabc.Iterator[tuple[int, int]]:
    """Yield a fresh ``(reader_fd, writer_fd)`` pipe pair and close both."""
    reader_fd, writer_fd = os.pipe()
    try:
        yield reader_fd, writer_fd
    finally:
        for fd in (reader_fd, writer_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


@_unix_only
@given(reader_blocking=st.booleans(), writer_blocking=st.booleans())
def test_blocking_guard_round_trips_prior_mode(
    *,
    reader_blocking: bool,
    writer_blocking: bool,
) -> None:
    """``engage`` forces blocking mode; ``restore`` returns the prior mode."""
    with _pipe_fds() as (reader_fd, writer_fd):
        os.set_blocking(reader_fd, reader_blocking)
        os.set_blocking(writer_fd, writer_blocking)

        guard = _BlockingModeGuard.engage(reader_fd=reader_fd, writer_fd=writer_fd)

        # While the pump owns the FDs they must both be blocking, whatever
        # their prior mode.
        assert os.get_blocking(reader_fd) is True, (
            "engage must leave the reader FD blocking while the pump owns it"
        )
        assert os.get_blocking(writer_fd) is True, (
            "engage must leave the writer FD blocking while the pump owns it"
        )
        assert guard.reader_was_blocking == reader_blocking, (
            "guard must capture the reader's prior blocking mode, expected "
            f"{reader_blocking}"
        )
        assert guard.writer_was_blocking == writer_blocking, (
            "guard must capture the writer's prior blocking mode, expected "
            f"{writer_blocking}"
        )

        guard.restore()

        assert os.get_blocking(reader_fd) == reader_blocking, (
            "restore must return the reader FD to its prior mode; now "
            f"{os.get_blocking(reader_fd)}, expected {reader_blocking}"
        )
        assert os.get_blocking(writer_fd) == writer_blocking, (
            "restore must return the writer FD to its prior mode; now "
            f"{os.get_blocking(writer_fd)}, expected {writer_blocking}"
        )


@_unix_only
@given(other_blocking=st.booleans(), fault_target=st.sampled_from(["reader", "writer"]))
def test_failed_engage_leaks_no_blocking_state(
    *,
    other_blocking: bool,
    fault_target: str,
) -> None:
    """A toggle failure rolls back any change, leaking no blocking state."""
    with _pipe_fds() as (reader_fd, writer_fd):
        # Force the faulted FD non-blocking so ``engage`` attempts to toggle it
        # and the injected fault fires; the other FD's mode is free.
        if fault_target == "reader":
            target_fd = reader_fd
            os.set_blocking(reader_fd, False)
            os.set_blocking(writer_fd, other_blocking)
        else:
            target_fd = writer_fd
            os.set_blocking(writer_fd, False)
            os.set_blocking(reader_fd, other_blocking)

        reader_initial = os.get_blocking(reader_fd)
        writer_initial = os.get_blocking(writer_fd)

        real_set_blocking = os.set_blocking

        def faulting_set_blocking(fd: int, blocking: bool) -> None:  # noqa: FBT001  # mirrors os.set_blocking's positional bool
            """Fail when the target FD is toggled to blocking; else delegate."""
            if fd == target_fd and blocking:
                msg = "injected toggle failure"
                raise OSError(msg)
            real_set_blocking(fd, blocking)

        # `monkeypatch` is function-scoped and so unavailable inside a
        # `@given` body; `patch.object` restores the global on every exit path
        # without the manual try/finally, and without the type: ignore that
        # rebinding an `os` attribute otherwise needs.
        with (
            mock.patch.object(os, "set_blocking", faulting_set_blocking),
            pytest.raises(OSError, match="injected toggle failure"),
        ):
            _BlockingModeGuard.engage(reader_fd=reader_fd, writer_fd=writer_fd)

        # No descriptor may be left in the transient blocking mode.
        assert os.get_blocking(reader_fd) == reader_initial, (
            "a failed engage must leak no blocking state on the reader; now "
            f"{os.get_blocking(reader_fd)}, expected {reader_initial}"
        )
        assert os.get_blocking(writer_fd) == writer_initial, (
            "a failed engage must leak no blocking state on the writer; now "
            f"{os.get_blocking(writer_fd)}, expected {writer_initial}"
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


class _FakeReader:
    """Minimal stand-in exposing a ``transport`` attribute."""

    def __init__(self, transport: object) -> None:
        """Wrap ``transport`` as the reader's public transport."""
        self.transport = transport


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
    reader = typ.cast("asyncio.StreamReader", _FakeReader(transport))

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
    reader = typ.cast("asyncio.StreamReader", _FakeReader(_ResumeOnlyTransport()))

    with _paused_reader(reader) as may_hand_off:
        assert may_hand_off is True, (
            "with no callbacks to race, the descriptor hand-off is safe"
        )


def test_paused_reader_undoes_a_half_applied_pause() -> None:
    """A pause that raises is corrected once, at the failure site."""
    transport = _FakeTransport(pause_raises=True)
    reader = typ.cast("asyncio.StreamReader", _FakeReader(transport))

    with _paused_reader(reader) as may_hand_off:
        assert may_hand_off is False, (
            "a pause that raised must report the hand-off as unsafe"
        )

    assert transport.pause_calls == 1, "the pause must be attempted exactly once"
    # A transport can set its paused flag before whatever raised, and the
    # Python fallback then reads a descriptor nothing is watching. The resume
    # runs at the failure site, not at block exit, so exactly one fires.
    assert transport.resume_calls == 1, (
        "a pause that raised must be undone, in case it half-applied"
    )


def _raise_oserror(**_kwargs: object) -> tuple[bool, bool]:
    """Stand in for ``_set_stream_fds_blocking`` failing to toggle a FD."""
    msg = "cannot switch descriptor to blocking mode"
    raise OSError(msg)


def test_run_rust_pump_falls_back_and_resumes_when_blocking_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking-toggle failure returns the Python-fallback signal and resumes."""
    resume_calls = {"count": 0}

    def fake_pause_reader_transport(
        reader: asyncio.StreamReader,
    ) -> _pipeline_stream_fds._ReaderPause:
        """Return a resume callback that records how often it is invoked."""
        del reader

        def _resume() -> None:
            """Record a resume invocation."""
            resume_calls["count"] += 1

        return _pipeline_stream_fds._ReaderPause(may_hand_off=True, resume=_resume)

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
        _raise_oserror,
    )

    reader = typ.cast("asyncio.StreamReader", object())
    handled = asyncio.run(
        _pipeline_streams._run_rust_pump(
            reader=reader,
            writer=None,
            reader_fd=1,
            writer_fd=2,
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
        lambda _reader: _pipeline_stream_fds._ReaderPause(may_hand_off=False),
    )
    monkeypatch.setattr(_pipeline_stream_fds._BlockingModeGuard, "engage", fake_engage)

    reader = typ.cast("asyncio.StreamReader", object())
    handled = asyncio.run(
        _pipeline_streams._pump_over_raw_fds(
            reader=reader,
            writer=None,
            reader_fd=1,
            writer_fd=2,
        )
    )

    assert handled is False, "a failed pause must report the Python-fallback signal"
    assert engaged["count"] == 0, (
        "a failed pause must return before engaging blocking mode"
    )
