"""`OSError`s crossing the PyO3 boundary carry a usable `errno`.

An extension that reports failures only in message text forces callers to parse
English to tell one failure from another, and message text is not a stable
interface. These tests pin the two properties that make the error branchable:
`errno` is populated, and the exception is the subclass that errno implies.

They isolate the conversion rather than reaching it through a pump: the pump
treats a broken pipe as non-fatal and drains, so most interesting errnos never
propagate through it. Calling the exported entry points with a deliberately
unusable descriptor reaches the conversion directly.

The conversion has a POSIX arm and a Windows arm, and they hand the number over
differently, so each arm has its own platform-scoped cases below.

Every expectation comes from outside the exception under test. Deriving one
from the raised exception would let a hard-coded or simply wrong native code
satisfy the assertion, which is no test at all. The POSIX arm names the `errno`
outright and takes `strerror` from `os.strerror`; the Windows arm performs the
same failing `ReadFile` through `ctypes` and reads the code back from
`GetLastError`.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import re
import sys
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib
    from types import ModuleType

# The two platform arms of the conversion carry different taxonomies, so each
# set of assertions is scoped to the platform it describes rather than the
# module being skipped wholesale. POSIX cases name an `errno` and the subclass
# CPython derives from it; the Windows cases name a `winerror` and the `errno`
# CPython derives from *that*, which is a different number for the same
# failure. Neither arm has a job that executes it today (see
# `docs/developers-guide.md`, "Preserving the operating-system error code"):
# POSIX execution arrives with the `extension-tests` job in #269, and native
# Windows runtime coverage is tracked by leynos/cuprum#277. Until then these
# encode the contract.
_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX errno values and the subclasses derived from them",
)
_windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="asserts Win32 winerror values and the errno derived from them",
)

# `pytest.raises(OSError)` needs a `match` (ruff PT011), but `strerror` comes
# from the C library and is translated under a non-English locale. Anchor on
# the `[Errno N]` prefix CPython formats itself, which no locale changes.
_ERRNO_PREFIX_RE = re.escape(f"[Errno {errno.EBADF}]")


@pytest.fixture(name="broken_pipe_fds")
def fixture_broken_pipe_fds() -> cabc.Iterator[tuple[int, int]]:
    """Yield ``(closed_read_fd, open_write_fd)`` for a pipe.

    The read end is closed before the test runs, so any attempt to read it
    fails with ``EBADF`` while the write end stays valid.

    Yields
    ------
    tuple[int, int]
        The closed read descriptor and open write descriptor.
    """
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        yield read_fd, write_fd
    finally:
        with contextlib.suppress(OSError):
            os.close(write_fd)


@pytest.fixture(name="directory_fd")
def fixture_directory_fd() -> cabc.Iterator[int]:
    """Yield a descriptor open on a directory, which cannot be read."""
    fd = os.open(".", os.O_RDONLY)
    try:
        yield fd
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


@_posix_only
def test_pump_error_reports_a_branchable_errno(
    rust_streams: ModuleType,
    broken_pipe_fds: tuple[int, int],
) -> None:
    """A failed pump reports `errno`, not just a message mentioning it.

    PyO3's own conversion builds the exception from a single argument — the
    error's `Display` string — and Python only fills in `errno` and `strerror`
    when given two or more. The number then survives in the text and nowhere a
    caller can branch on, which is the defect this pins.
    """
    closed_read_fd, write_fd = broken_pipe_fds

    with pytest.raises(OSError, match=_ERRNO_PREFIX_RE) as excinfo:
        rust_streams.rust_pump_stream(closed_read_fd, write_fd)

    assert excinfo.value.errno == errno.EBADF, (
        f"a closed descriptor must report EBADF in errno, found {excinfo.value.errno!r}"
    )
    assert excinfo.value.strerror == os.strerror(errno.EBADF), (
        "strerror must be the system's own description of EBADF, obtained "
        "here from CPython rather than from the exception under test; found "
        f"{excinfo.value.strerror!r}"
    )


@_posix_only
def test_consume_error_reports_a_branchable_errno(
    rust_streams: ModuleType,
    broken_pipe_fds: tuple[int, int],
) -> None:
    """The consume entry point preserves `errno` on the same terms.

    This pins the conversion contract: the exact POSIX number, which is why it
    skips on Windows.
    ``TestRustConsumeStream.test_propagates_io_errors`` in
    `test_rust_streams.py` covers the consume entry point's own I/O-failure
    behaviour beside the rest of that entry point's coverage, and accepts
    either `EBADF` or `EINVAL` so it can run on every platform.
    """
    closed_read_fd, _write_fd = broken_pipe_fds

    with pytest.raises(OSError, match=_ERRNO_PREFIX_RE) as excinfo:
        rust_streams.rust_consume_stream(closed_read_fd)

    assert excinfo.value.errno == errno.EBADF, (
        f"expected EBADF, found {excinfo.value.errno!r}"
    )


@_posix_only
def test_the_exception_subclass_follows_the_errno(
    rust_streams: ModuleType,
    directory_fd: int,
) -> None:
    """The raised type is the subclass the errno implies.

    Reading a directory yields `EISDIR`, whose Python subclass is
    `IsADirectoryError`. Passing `(errno, strerror)` lets CPython select that
    subclass itself, which is both the authoritative mapping and the behaviour
    the previous conversion reached for through a parallel `ErrorKind` table.
    """
    with pytest.raises(IsADirectoryError) as excinfo:
        rust_streams.rust_consume_stream(directory_fd)

    assert excinfo.value.errno == errno.EISDIR, (
        f"expected EISDIR, found {excinfo.value.errno!r}"
    )


@_posix_only
def test_the_message_states_the_error_number_once(
    rust_streams: ModuleType,
    broken_pipe_fds: tuple[int, int],
) -> None:
    """`strerror` carries no duplicate of the number Python already prefixes.

    Rust renders a raw OS error as ``"{strerror} (os error {code})"``. Passing
    that whole string through would render as ``"[Errno 9] Bad file descriptor
    (os error 9)"``, stating the number twice.
    """
    closed_read_fd, _write_fd = broken_pipe_fds

    with pytest.raises(OSError, match=_ERRNO_PREFIX_RE) as excinfo:
        rust_streams.rust_consume_stream(closed_read_fd)

    assert "os error" not in str(excinfo.value), (
        "the Rust-side suffix must be stripped, leaving Python's own "
        f"[Errno N] prefix as the only mention; found {str(excinfo.value)!r}"
    )
    assert str(excinfo.value).startswith(f"[Errno {errno.EBADF}]"), (
        f"expected a normal OSError rendering, found {str(excinfo.value)!r}"
    )


def _winerror_of(exc: OSError) -> int | None:
    """Read the Win32 code CPython attaches to a Windows ``OSError``.

    ``OSError.winerror`` exists only on Windows, so a type check run on any
    other platform does not know the attribute. The suppression is confined to
    this one accessor rather than repeated at each use.

    Returns
    -------
    int | None
        The native Win32 error code, when CPython attached one.
    """
    return exc.winerror  # ty: ignore[unresolved-attribute]


def _win32_readfile_error(handle: int) -> int:
    """Return the Win32 code a direct ``ReadFile`` on ``handle`` reports.

    This is the oracle the boundary is measured against. It issues the same
    system call the extension does — a Windows stream handle is a
    ``std::fs::File``, whose ``read`` is ``ReadFile`` — but through ``ctypes``,
    so nothing it returns passes through the code under test. A hard-coded or
    mistaken native code on the Rust side therefore fails the comparison.

    ``use_last_error`` makes ``ctypes`` stash ``GetLastError`` immediately on
    return, before the interpreter can make a call of its own and overwrite it.

    Parameters
    ----------
    handle : int
        A native ``HANDLE`` that cannot be read.

    Returns
    -------
    int
        The ``GetLastError`` code the failed read reported.

    Examples
    --------
    >>> _win32_readfile_error(write_only_handle)  # doctest: +SKIP
    5
    """
    kernel32 = ctypes.WinDLL(  # ty: ignore[unresolved-attribute]
        "kernel32",
        use_last_error=True,
    )
    read_file = kernel32.ReadFile
    # A HANDLE is pointer-sized; without argtypes ctypes would pass it as a C
    # int and truncate it on 64-bit Windows.
    read_file.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    )
    read_file.restype = ctypes.c_int
    taken = ctypes.c_uint32(0)
    succeeded = read_file(
        ctypes.c_void_p(handle),
        ctypes.create_string_buffer(1),
        1,
        ctypes.byref(taken),
        None,
    )
    assert not succeeded, (
        "the probe read must fail, or it cannot say which code the extension "
        "should have reported"
    )
    return ctypes.get_last_error()  # ty: ignore[unresolved-attribute]


def _win32_oracle(code: int) -> OSError:
    """Build the ``OSError`` CPython itself produces for a Win32 ``code``.

    ``ctypes.WinError`` looks the message up with ``FormatMessage`` and hands
    the code to ``OSError`` as ``winerror``, so CPython derives ``errno`` and
    selects the subclass. That makes it the authoritative statement of what the
    extension's exception should look like, built from the independently
    obtained code rather than from the exception itself.

    Parameters
    ----------
    code : int
        A Win32 error code.

    Returns
    -------
    OSError
        The exception CPython derives from ``code``.

    Examples
    --------
    >>> _win32_oracle(5).errno  # doctest: +SKIP
    13
    """
    return ctypes.WinError(code)  # ty: ignore[unresolved-attribute]


@pytest.fixture(name="write_only_handle")
def fixture_write_only_handle(tmp_path: pathlib.Path) -> cabc.Iterator[int]:
    """Yield a native Windows handle open for writing, which cannot be read.

    The Windows entry points take a native ``HANDLE`` rather than a C runtime
    descriptor, so the descriptor is converted with ``msvcrt.get_osfhandle``.
    ``ReadFile`` on a handle opened ``GENERIC_WRITE`` fails, which is what puts
    a Win32 code on the ``io::Error`` the conversion then has to preserve.

    Yields
    ------
    int
        The native handle for the write-only temporary file.
    """
    msvcrt = pytest.importorskip("msvcrt", reason="Windows-only handle conversion")
    fd = os.open(tmp_path / "write-only.bin", os.O_WRONLY | os.O_CREAT)
    try:
        yield msvcrt.get_osfhandle(fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


@_windows_only
def test_windows_failures_carry_the_native_code_as_winerror(
    rust_streams: ModuleType,
    write_only_handle: int,
) -> None:
    """A Win32 failure arrives as `winerror`, with the rest derived from it.

    On Windows `raw_os_error` is a `GetLastError` code, not an `errno`. Handing
    it over where Python expects an `errno` would assign an unrelated number,
    leave `winerror` unset, and pick the subclass from the wrong value. The
    five-argument form passes the native code as `winerror`; CPython then
    derives `errno` from it and selects the subclass from the derived value.

    None of the expectations come from the exception. The same read is issued
    first through `ctypes`, and the code it reports is what the extension is
    held to; `errno`, `strerror` and the subclass then come from the `OSError`
    CPython derives from that code. An extension that reported a constant, or
    the wrong code, or a code it never obtained from the failure, fails here —
    which is exactly what deriving the expectations from the raised `winerror`
    could not detect.
    """
    expected = _win32_readfile_error(write_only_handle)
    oracle = _win32_oracle(expected)

    with pytest.raises(OSError, match=re.escape(f"[WinError {expected}]")) as excinfo:
        rust_streams.rust_consume_stream(write_only_handle)

    raised = excinfo.value
    assert _winerror_of(raised) == expected, (
        f"ReadFile reported {expected}, so winerror must carry that code; "
        f"found {_winerror_of(raised)!r}"
    )
    assert type(raised) is type(oracle), (
        f"winerror {expected} implies {type(oracle).__name__}, "
        f"found {type(raised).__name__}"
    )
    assert raised.errno == oracle.errno, (
        f"errno must be the one CPython derives from winerror {expected}: "
        f"expected {oracle.errno!r}, found {raised.errno!r}"
    )
    assert raised.strerror == oracle.strerror, (
        f"strerror must be the system's description of winerror {expected}: "
        f"expected {oracle.strerror!r}, found {raised.strerror!r}"
    )
    assert "os error" not in str(raised), (
        "the Rust-side suffix must be stripped, leaving Python's own "
        f"[WinError N] prefix as the only mention; found {str(raised)!r}"
    )
