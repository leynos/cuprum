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
    import collections.abc as cabc
    from types import ModuleType


_windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="asserts the Windows-only raw-handle rejection boundary",
)
_ERROR_INVALID_HANDLE = 6
# One past the CRT's signed 32-bit descriptor range.
_ABOVE_I32_MAX = 1 << 31
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
def test_consume_rejects_an_out_of_range_crt_descriptor(
    rust_streams: ModuleType,
) -> None:
    """The Python wrapper refuses a descriptor the CRT would fast-fail on.

    Passing an out-of-range descriptor to ``_read`` invokes the invalid
    parameter handler, whose default ``_invoke_watson`` terminates the process
    with ``STATUS_STACK_BUFFER_OVERRUN`` instead of returning ``EBADF``. The
    rejection therefore has to happen before the CRT sees the value, so the
    descriptor must exceed the CRT's signed 32-bit range: 1,000,000 is a
    perfectly good ``int`` and would be duplicated and passed straight to
    ``_read``.
    """
    with pytest.raises(ValueError, match="file descriptor out of range"):
        rust_streams.rust_consume_stream(_ABOVE_I32_MAX)


@_windows_only
def test_consume_reports_a_buffer_error_before_a_descriptor_error(
    rust_streams: ModuleType,
) -> None:
    """An invalid buffer is named even when the descriptor is also unusable.

    Both arguments are invalid, so the message identifies which check ran
    first. Reversing the order would put ``os.dup`` in front of a rejection the
    native entry point documents as ``ValueError``.
    """
    with pytest.raises(ValueError, match="buffer_size"):
        rust_streams.rust_consume_stream(_ABOVE_I32_MAX, buffer_size=0)


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
def test_binary_consume_preserves_crlf_and_ctrl_z(
    rust_streams: ModuleType,
) -> None:
    """Windows consume keeps raw bytes and leaves the caller in text mode."""
    import msvcrt

    setmode = typ.cast(
        "cabc.Callable[[int, int], int]",
        getattr(msvcrt, "setmode"),  # ruff: ignore[get-attr-with-constant]  # Windows-only API.
    )
    text_mode: int = os.O_TEXT  # ty: ignore[unresolved-attribute]  # Windows-only constant.
    binary_mode: int = os.O_BINARY  # ty: ignore[unresolved-attribute]  # Windows-only constant.
    read_fd, write_fd = os.pipe()
    try:
        setmode(read_fd, text_mode)
        setmode(write_fd, binary_mode)
        payload = b"before\r\n\x1aafter"
        assert os.write(write_fd, payload) == len(payload)
        os.close(write_fd)

        output = rust_streams.rust_consume_stream(read_fd)

        assert output == payload.decode("utf-8", errors="replace"), (
            "binary native consume must preserve CRLF and Ctrl-Z bytes"
        )
        assert setmode(read_fd, text_mode) == text_mode, (
            "consume must not change the caller descriptor's text mode"
        )
    finally:
        for fd in (read_fd, write_fd):
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
