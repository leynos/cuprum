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
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed interpreter and source.
import sys
import typing as typ

import pytest

from cuprum import _streams_windows
from cuprum.unittests._rust_stream_test_support import (
    INVALID_FD_ERRNOS,
    INVALID_FD_MESSAGE_RE,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType


_windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="asserts the Windows-only raw-handle rejection boundary",
)
_ERROR_ACCESS_DENIED = 5
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
        # An unusable descriptor is EBADF on POSIX and WinError 6 on Windows;
        # the shared helper matches both by their numeric prefix, which is what
        # CPython formats itself, rather than locale-dependent strerror text.
        with pytest.raises(OSError, match=INVALID_FD_MESSAGE_RE) as closed_duplicate:
            os.fstat(duplicate_fds[0])
        assert closed_duplicate.value.errno in INVALID_FD_ERRNOS
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


# The import comes first: importing the extension can itself create and close
# CRT descriptors, so a descriptor created before the import could be silently
# shadowed by a freed number the import reused.
_UNUSABLE_DESCRIPTOR_SOURCE = (
    "import os, sys\n"
    "from cuprum import _rust_backend_native as native\n"
    "mode = sys.argv[1]\n"
    "reader, writer = os.pipe()\n"
    "if mode == 'closed':\n"
    " os.close(reader)\n"
    " value = reader\n"
    "else:\n"
    " value = writer\n"
    "try:\n"
    " native.rust_consume_stream(value)\n"
    "except OSError as error:\n"
    " print('OSError', error.errno, error.winerror)\n"
    "else:\n"
    " print('NO-ERROR')\n"
    "finally:\n"
    " for fd in (reader, writer):\n"
    "  try:\n"
    "   os.close(fd)\n"
    "  except OSError:\n"
    "   pass\n"
)


def _derived_errno(winerror: int) -> int | None:
    """Return the ``errno`` CPython derives for a ``winerror``.

    Asking CPython rather than restating the mapping keeps the expected value
    honest: if the extension ever passed its Win32 code in the ``errno`` slot
    instead, the two fields would disagree with this and the test would fail.

    Parameters
    ----------
    winerror : int
        Win32 error code the native boundary reports.

    Returns
    -------
    int | None
        The ``errno`` CPython associates with that code.
    """
    return OSError(0, "probe", None, winerror, None).errno


@_windows_only
@pytest.mark.parametrize(
    ("mode", "expected_winerror"),
    [
        # A closed descriptor is rejected by the CRT's `_dup`, which reports
        # `EBADF`; the classifier rewrites that to `ERROR_INVALID_HANDLE` so
        # the Win32 reading is uniform with every other native error.
        ("closed", _ERROR_INVALID_HANDLE),
        # An open but unreadable descriptor duplicates successfully and then
        # fails inside `ReadFile` with `ERROR_ACCESS_DENIED`. It is not an
        # invalid-handle condition and must not be folded into the code above;
        # nor is it the CRT's `EACCES`, which shares that number only on this
        # platform's errno table and means nothing to Win32.
        ("write-only", _ERROR_ACCESS_DENIED),
    ],
    ids=("closed", "write-only"),
)
# The child bound below only does its job if this test outlives it. The
# repository default is 30 seconds, which a wedged child could consume
# entirely before pytest-timeout fired; on Windows its default method
# terminates the whole interpreter, so the suite would abort without the
# child-specific diagnostic. The child is therefore bounded at 40 seconds and
# this marker widens the test's envelope to 60, leaving the kill-and-reap
# steps 20 seconds to report.
@pytest.mark.timeout(60)
def test_direct_consume_reports_an_unusable_descriptor(
    mode: str,
    expected_winerror: int,
    rust_streams: ModuleType,
) -> None:
    """An unusable descriptor must return an error, not abort Python.

    The CRT treats an unreadable descriptor as an invalid parameter, and its
    default handler terminates the process. The native boundary installs a
    continuing handler, so the same descriptor must surface as an ``OSError``
    — the two modes are not interchangeable, which is why each names its own
    expected code. This runs in a child process because the regression it
    guards is the child dying: an in-process assertion could not report that
    outcome. The descriptor is derived inside the child so the number refers to
    that process's descriptor table, not this one's.

    The two error fields are asserted separately. On Windows the native
    boundary passes the Win32 code in the ``winerror`` slot and CPython derives
    ``errno`` from it, so a check that accepted the value in either field could
    not tell a correct hand-off from one that put a Win32 number where an
    ``errno`` belongs.
    """
    native = rust_streams._load_native()
    assert hasattr(native, "rust_consume_stream"), (
        "the exported native consume entry must stay reachable for direct callers"
    )
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed interpreter and source.
        [sys.executable, "-c", _UNUSABLE_DESCRIPTOR_SOURCE, mode],
        shell=False,
        capture_output=True,
        encoding="utf-8",
        check=False,
        # The child imports the extension and opens a pipe, so it should finish
        # in well under a second. A bound keeps a wedged child from hanging the
        # suite to pytest-timeout's whole budget and reports it as this test;
        # it must stay below that budget, which the marker above widens.
        timeout=40,
    )
    assert completed.returncode == 0, (
        f"consume on a {mode} descriptor must not terminate the interpreter; "
        f"exit {completed.returncode}, stdout={completed.stdout!r}, "
        f"stderr={completed.stderr!r}"
    )
    reported = completed.stdout.split()
    # Require the full field count before indexing, so a short or unexpected
    # line reports the observed output instead of raising IndexError.
    assert len(reported) >= 3, (
        f"consume on a {mode} descriptor must report the error's errno and "
        f"winerror, got {completed.stdout!r}"
    )
    assert reported[0] == "OSError", (
        f"consume on a {mode} descriptor must report OSError, got {completed.stdout!r}"
    )
    assert reported[2] == str(expected_winerror), (
        f"a {mode} descriptor must report WinError {expected_winerror}, got "
        f"errno={reported[1]!r} winerror={reported[2]!r}"
    )
    assert reported[1] == str(_derived_errno(expected_winerror)), (
        f"a {mode} descriptor must carry the errno CPython derives for "
        f"winerror {expected_winerror}, not the Win32 code itself, got "
        f"errno={reported[1]!r} winerror={reported[2]!r}"
    )


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
