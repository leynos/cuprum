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
import ctypes
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
_ERROR_INVALID_HANDLE = 6


def _assert_invalid_windows_handle(raw_handle: int) -> None:
    """Check a handle without acquiring ownership or blocking."""
    kernel32 = ctypes.WinDLL(  # ty: ignore[unresolved-attribute]  # Windows-only ctypes API.
        "kernel32", use_last_error=True
    )
    get_handle_information = kernel32.GetHandleInformation
    get_handle_information.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    )
    get_handle_information.restype = ctypes.c_int
    flags = ctypes.c_ulong()
    handle_is_open = get_handle_information(
        ctypes.c_void_p(raw_handle), ctypes.byref(flags)
    )
    assert not handle_is_open, "rejected pump must close the transferred writer"
    get_last_error = ctypes.get_last_error  # ty: ignore[unresolved-attribute]  # Windows API.
    assert get_last_error() == _ERROR_INVALID_HANDLE, (
        "closed writer must report ERROR_INVALID_HANDLE"
    )


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
    native = rust_streams._load_native()
    reader_fd, reader_writer_fd = os.pipe()
    writer_reader_fd, writer_fd = os.pipe()
    reader_handle = _streams_rs._convert_fd_for_platform(reader_fd)
    writer_handle = _streams_rs._duplicate_windows_handle(
        _streams_rs._convert_fd_for_platform(writer_fd)
    )
    writer_transferred = False
    try:
        writer_transferred = True
        with pytest.raises(OSError, match=_RAW_HANDLE_MESSAGE):
            native.rust_pump_stream(reader_handle, writer_handle)

        _assert_invalid_windows_handle(writer_handle)
    finally:
        for fd in (reader_fd, reader_writer_fd, writer_reader_fd, writer_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
        if not writer_transferred:
            with contextlib.suppress(OSError):
                _streams_rs._close_windows_handle(writer_handle)
