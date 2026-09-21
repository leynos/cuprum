"""Windows raw-handle rejection at the PyO3 stream boundary.

The synchronous native adapter accepts only capabilities whose non-overlapped
mode was established at creation or in an audited Rust hand-off. A bare Python
integer cannot establish that property, so the exported PyO3 helpers reject
raw Windows handles before they construct the capability. The native-I/O crate
tests the accepted ``synchronous_pipe`` path directly.

Example
-------
pytest cuprum/unittests/test_rust_errno_windows.py
"""

from __future__ import annotations

import contextlib
import os
import sys
import typing as typ

import pytest

from cuprum import _streams_rs

if typ.TYPE_CHECKING:
    from types import ModuleType


_windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="asserts the Windows-only raw-handle rejection boundary",
)
_RAW_HANDLE_MESSAGE = "do not accept raw Windows handles"


@_windows_only
def test_consume_rejects_raw_windows_handle(rust_streams: ModuleType) -> None:
    """The safe Python entry point cannot assert synchronous I/O from an int."""
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(OSError, match=_RAW_HANDLE_MESSAGE):
            rust_streams.rust_consume_stream(read_fd)
    finally:
        for fd in (read_fd, write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


@_windows_only
def test_pump_rejects_raw_windows_handles(rust_streams: ModuleType) -> None:
    """The raw writer transfer is consumed while the unsupported call fails."""
    reader_fd, reader_writer_fd = os.pipe()
    writer_reader_fd, writer_fd = os.pipe()
    reader_handle = _streams_rs._convert_fd_for_platform(reader_fd)
    writer_handle = _streams_rs._duplicate_windows_handle(
        _streams_rs._convert_fd_for_platform(writer_fd)
    )
    try:
        with pytest.raises(OSError, match=_RAW_HANDLE_MESSAGE):
            rust_streams.rust_pump_stream(reader_handle, writer_handle)
    finally:
        for fd in (reader_fd, reader_writer_fd, writer_reader_fd, writer_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
