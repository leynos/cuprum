"""Invalid native reader arguments must not strand an owned writer duplicate."""

from __future__ import annotations

import contextlib
import os
from types import SimpleNamespace
from unittest import mock

import pytest

from cuprum import _streams_rs


def _close_duplicate(fd: int) -> None:
    """Close a duplicate the native pump may already have consumed."""
    with contextlib.suppress(OSError):
        os.close(fd)


@pytest.mark.parametrize(
    ("reader", "error"),
    [
        (-1, ValueError),
        (1 << 64, OverflowError),
        pytest.param(
            1 << 31,
            ValueError,
            marks=pytest.mark.skipif(
                os.name == "nt", reason="Windows handles are pointer-sized"
            ),
        ),
    ],
)
def test_invalid_reader_closes_duplicate_before_native_call(
    monkeypatch: pytest.MonkeyPatch,
    reader: int,
    error: type[Exception],
) -> None:
    """ABI validation fails before transferring the only writer owner."""
    with contextlib.ExitStack() as stack:
        source, sink = os.pipe()
        stack.callback(os.close, source)
        stack.callback(os.close, sink)
        duplicate = os.dup(sink)
        stack.callback(_close_duplicate, duplicate)
        native = mock.Mock(return_value=0)
        monkeypatch.setattr(
            _streams_rs,
            "_load_native",
            lambda: SimpleNamespace(rust_pump_stream=native),
        )
        monkeypatch.setattr(_streams_rs, "_convert_fd_for_platform", lambda fd: fd)
        with pytest.raises(error):
            _streams_rs.rust_pump_stream(reader, duplicate)
        native.assert_not_called()
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(duplicate)
        os.fstat(sink)
