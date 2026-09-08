"""Invalid native reader arguments must not strand an owned writer duplicate."""

import contextlib
import os
from types import SimpleNamespace
from unittest import mock

import pytest

from cuprum import _streams_rs


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
    source, sink = os.pipe()
    duplicate = os.dup(sink)
    duplicate_closed = False
    native = mock.Mock(return_value=0)
    monkeypatch.setattr(
        _streams_rs, "_load_native", lambda: SimpleNamespace(rust_pump_stream=native)
    )
    monkeypatch.setattr(_streams_rs, "_convert_fd_for_platform", lambda fd: fd)
    try:
        with pytest.raises(error):
            _streams_rs.rust_pump_stream(reader, duplicate)
        native.assert_not_called()
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(duplicate)
        duplicate_closed = True
        os.fstat(sink)
    finally:
        os.close(source)
        os.close(sink)
        if not duplicate_closed:
            with contextlib.suppress(OSError):
                os.close(duplicate)
