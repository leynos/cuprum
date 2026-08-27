"""The Windows arm of the `errno` contract at the PyO3 boundary.

On Windows the extension's `io::Error` carries a `GetLastError` code rather
than a POSIX `errno`, so the conversion has a second arm: it hands the native
number over as `winerror` and lets CPython derive `errno` and the exception
subclass from that. `test_rust_errno.py` holds the POSIX arm and the shared
rationale for why the conversion is pinned at all.

Every expectation comes from outside the exception under test. The same failing
`ReadFile` is issued first through `ctypes`, and the code it reports is what the
extension is held to; deriving the expectation from the raised exception would
let a hard-coded or simply wrong native code satisfy the assertion.

No job executes this arm today — native Windows runtime coverage is tracked by
leynos/cuprum#277 (see `docs/developers-guide.md`, "Preserving the operating-
system error code") — so until then these cases encode the contract.

Example
-------
pytest cuprum/unittests/test_rust_errno_windows.py
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import sys
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib
    from types import ModuleType

_windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="asserts Win32 winerror values and the errno derived from them",
)


def _winerror_of(exc: OSError) -> int | None:
    """Read the Win32 code CPython attaches to a Windows ``OSError``.

    ``OSError.winerror`` exists only on Windows, so a type check run on any
    other platform does not know the attribute. The suppression is confined to
    this one accessor rather than repeated at each use.

    Returns
    -------
    int | None
        The Win32 error code, when CPython attaches one to the exception.
    """
    return exc.winerror  # ty: ignore[unresolved-attribute]


def _win32_readfile_error(fd: int) -> int:
    """Return the Win32 code a direct ``ReadFile`` on ``fd`` reports.

    This is the oracle the boundary is measured against. It issues the same
    system call the extension does — a Windows stream handle is a
    ``std::fs::File``, whose ``read`` is ``ReadFile`` — but through ``ctypes``,
    so nothing it returns passes through the code under test. A hard-coded or
    mistaken native code on the Rust side therefore fails the comparison.

    ``use_last_error`` makes ``ctypes`` stash ``GetLastError`` immediately on
    return, before the interpreter can make a call of its own and overwrite it.

    Parameters
    ----------
    fd : int
        A C runtime descriptor that cannot be read.

    Returns
    -------
    int
        The ``GetLastError`` code the failed read reported.

    Examples
    --------
    >>> _win32_readfile_error(write_only_handle)  # doctest: +SKIP
    5
    """
    msvcrt = pytest.importorskip("msvcrt", reason="Windows-only handle conversion")
    handle = msvcrt.get_osfhandle(fd)
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
    """Yield a Windows CRT descriptor open for writing, which cannot be read.

    The extension owns the conversion to a native ``HANDLE``. ``ReadFile`` on
    the resulting handle fails because it was opened ``GENERIC_WRITE``, which
    is what puts a Win32 code on the ``io::Error`` the conversion must retain.

    Yields
    ------
    int
        The C runtime descriptor for the write-only file.
    """
    fd = os.open(tmp_path / "write-only.bin", os.O_WRONLY | os.O_CREAT)
    try:
        yield fd
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


@_windows_only
def test_windows_failures_carry_the_native_code_as_winerror(
    rust_streams: ModuleType,
    write_only_handle: int,
) -> None:
    """A Win32 failure arrives as `winerror`, with the rest derived from it.

    Parameters
    ----------
    rust_streams : ModuleType
        The compiled Rust streams extension module.
    write_only_handle : int
        A native Windows ``HANDLE`` opened for writing, which cannot be read.

    Notes
    -----
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
