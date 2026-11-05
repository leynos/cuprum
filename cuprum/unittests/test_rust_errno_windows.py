"""Windows CRT consume errors and synchronous-handle pump rejection.

The native pump accepts only capabilities whose non-overlapped mode was
established at creation or in an audited Rust hand-off. A bare Python integer
cannot establish that property, so the PyO3 pump rejects raw Windows handles.
Consume has a separate CRT descriptor boundary and uses the synchronous CRT
``_read`` API. The native-I/O crate tests the accepted ``synchronous_pipe``
pump path directly.

Example
-------
pytest cuprum/unittests/test_rust_errno_windows.py
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import sys
import typing as typ

import pytest

from cuprum import _streams_windows

if typ.TYPE_CHECKING:
    from types import ModuleType


_windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="asserts the Windows-only raw-handle rejection boundary",
)
_ERROR_INVALID_HANDLE = 6
_RAW_HANDLE_MESSAGE = "do not accept raw Windows handles"


def _assert_invalid_windows_handle(raw_handle: int) -> None:
    """Check closure without ownership or blocking.

    Concurrent reuse of the same process-local handle value can race this probe.
    """
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
    ctypes.set_last_error(0)  # ty: ignore[unresolved-attribute]  # Windows API.
    handle_is_open = get_handle_information(
        ctypes.c_void_p(raw_handle), ctypes.byref(flags)
    )
    assert not handle_is_open, "rejected pump must close the transferred writer"
    get_last_error = ctypes.get_last_error  # ty: ignore[unresolved-attribute]  # Windows API.
    assert get_last_error() == _ERROR_INVALID_HANDLE, (
        "closed writer must report ERROR_INVALID_HANDLE"
    )


@_windows_only
def test_consume_maps_an_invalid_crt_descriptor(
    rust_streams: ModuleType,
) -> None:
    """Direct native consume preserves the CRT's invalid-handle code."""
    native = rust_streams._load_native()
    with pytest.raises(OSError, match="WinError 6") as excinfo:
        native.rust_consume_stream(1_000_000)

    assert excinfo.value.winerror == _ERROR_INVALID_HANDLE
    assert excinfo.value.errno == errno.EBADF


@_windows_only
def test_binary_mode_setup_failure_closes_the_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The temporary descriptor closes even when CRT mode setup fails."""
    import msvcrt

    reader_fd, writer_fd = os.pipe()
    duplicate_fds: list[int] = []
    original_dup = os.dup

    def capture_duplicate(fd: int) -> int:
        """Record the owned copy so cleanup remains observable."""
        duplicate_fd = original_dup(fd)
        duplicate_fds.append(duplicate_fd)
        return duplicate_fd

    def reject_binary_mode(_fd: int, _mode: int) -> int:
        """Inject the CRT setup failure after duplication succeeds."""
        raise OSError(errno.EINVAL, "injected setmode failure")

    monkeypatch.setattr(_streams_windows.os, "dup", capture_duplicate)
    monkeypatch.setattr(msvcrt, "setmode", reject_binary_mode)
    try:
        with (
            pytest.raises(OSError, match="injected setmode failure"),
            _streams_windows._duplicate_binary_reader(reader_fd),
        ):
            pytest.fail("binary-mode setup should fail before yielding")

        assert len(duplicate_fds) == 1, "the helper must duplicate exactly once"
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(duplicate_fds[0])
    finally:
        for fd in (reader_fd, writer_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


@_windows_only
def test_pump_rejects_raw_windows_handles(rust_streams: ModuleType) -> None:
    """The raw writer transfer is consumed while the unsupported call fails."""
    reader_fd: int | None = None
    reader_writer_fd: int | None = None
    writer_reader_fd: int | None = None
    writer_fd: int | None = None
    writer_handle: int | None = None
    writer_transferred = False
    try:
        native = rust_streams._load_native()
        reader_fd, reader_writer_fd = os.pipe()
        writer_reader_fd, writer_fd = os.pipe()
        reader_handle = _streams_windows._convert_fd_for_platform(reader_fd)
        writer_handle = _streams_windows._duplicate_windows_handle(
            _streams_windows._convert_fd_for_platform(writer_fd)
        )

        # PyO3 converts both handles to i64 before Rust can adopt the writer.
        i64_max = (1 << 63) - 1
        if not (0 <= reader_handle <= i64_max and 0 <= writer_handle <= i64_max):
            pytest.skip("Windows handle is outside the native PyO3 i64 range")

        # Rust then adopts and closes the transferred writer before rejecting
        # this raw-handle call.
        writer_transferred = True
        with pytest.raises(OSError, match=_RAW_HANDLE_MESSAGE):
            native.rust_pump_stream(reader_handle, writer_handle)

        _assert_invalid_windows_handle(writer_handle)
    finally:
        for fd in (reader_fd, reader_writer_fd, writer_reader_fd, writer_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        if writer_handle is not None and not writer_transferred:
            with contextlib.suppress(OSError):
                _streams_windows._close_windows_handle(writer_handle)


@pytest.mark.parametrize(
    ("reader_fd", "buffer_size", "error_message"),
    [
        (0, 0, "buffer_size must be greater than zero"),
        (-1, 65536, "file handle must be non-negative"),
    ],
    ids=("buffer-size", "reader-handle"),
)
@_windows_only
def test_pump_closes_writer_after_early_validation_failure(
    rust_streams: ModuleType,
    reader_fd: int,
    buffer_size: int,
    error_message: str,
) -> None:
    """Later buffer or reader validation failures close the adopted writer."""
    writer_reader_fd: int | None = None
    writer_fd: int | None = None
    writer_handle: int | None = None
    writer_transferred = False
    try:
        native = rust_streams._load_native()
        writer_reader_fd, writer_fd = os.pipe()
        writer_handle = _streams_windows._duplicate_windows_handle(
            _streams_windows._convert_fd_for_platform(writer_fd)
        )

        if not 0 <= writer_handle <= (1 << 63) - 1:
            pytest.skip("Windows handle is outside the native PyO3 i64 range")

        writer_transferred = True
        with pytest.raises(ValueError, match=error_message):
            native.rust_pump_stream(reader_fd, writer_handle, buffer_size)

        _assert_invalid_windows_handle(writer_handle)
    finally:
        for fd in (writer_reader_fd, writer_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        if writer_handle is not None and not writer_transferred:
            with contextlib.suppress(OSError):
                _streams_windows._close_windows_handle(writer_handle)
