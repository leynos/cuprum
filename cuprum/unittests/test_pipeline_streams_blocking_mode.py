"""Fault-injection tests for the Rust pump's blocking-mode seam.

Before `cuprum._pipeline_streams` hands the raw pipe descriptors to the Rust
pump it switches them to blocking mode through `_BlockingModeGuard`, which must
restore their prior mode afterwards and leak nothing when the switch itself
fails part-way. These tests inject faults into that seam to pin the
partial-failure behaviour #74 calls out. The reader-pause seam is exercised
separately in `test_pipeline_streams_fd_lifecycle`.
"""

from __future__ import annotations

import contextlib
import os
import sys
import typing as typ
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum._pipeline_stream_fds import _BlockingModeGuard

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_UNIX_ONLY = pytest.mark.skipif(
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


@_UNIX_ONLY
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


@_UNIX_ONLY
@given(
    other_blocking=st.booleans(),
    fault_target=st.sampled_from(["reader", "writer"]),
    fault_error=st.sampled_from([OSError, ValueError]),
)
def test_failed_engage_leaks_no_blocking_state(
    *,
    other_blocking: bool,
    fault_target: str,
    fault_error: type[Exception],
) -> None:
    """A toggle failure rolls back any change, leaking no blocking state.

    Both refusals are exercised because ``_restore_stream_fd_blocking``
    suppresses ``OSError`` and ``ValueError`` alike, so the rollback has to
    cover the same pair: rolling back on only one of them would leave the
    reader switched to blocking mode with no guard in existence to restore it.
    """
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
                raise fault_error(msg)
            real_set_blocking(fd, blocking)

        # `monkeypatch` is function-scoped and so unavailable inside a
        # `@given` body; `patch.object` restores the global on every exit path
        # without the manual try/finally, and without the type: ignore that
        # rebinding an `os` attribute otherwise needs.
        with (
            mock.patch.object(os, "set_blocking", faulting_set_blocking),
            pytest.raises(fault_error, match="injected toggle failure"),
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
