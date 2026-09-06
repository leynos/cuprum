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
differently. This module holds the POSIX arm;
`test_rust_errno_windows.py` holds the Windows one, which shares none of the
fixtures here.

Every expectation comes from outside the exception under test. Deriving one
from the raised exception would let a hard-coded or simply wrong native code
satisfy the assertion, which is no test at all. The POSIX arm names the `errno`
outright and takes `strerror` from `os.strerror`.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import sys
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType

# The assertions are scoped to the platform they describe rather than the module
# being skipped wholesale, because the two arms carry different taxonomies: the
# POSIX cases below name an `errno` and the subclass CPython derives from it.
# The `extension-tests` job executes them on Linux, while
# `extension-tests-windows` executes the separate Win32 taxonomy in
# `test_rust_errno_windows.py`.
_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX errno values and the subclasses derived from them",
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
        The closed read descriptor and the open write descriptor.
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

    Parameters
    ----------
    rust_streams : ModuleType
        The compiled Rust streams extension module.
    broken_pipe_fds : tuple[int, int]
        A closed read descriptor and an open write descriptor.

    Notes
    -----
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

    Parameters
    ----------
    rust_streams : ModuleType
        The compiled Rust streams extension module.
    broken_pipe_fds : tuple[int, int]
        A closed read descriptor and an open write descriptor.

    Notes
    -----
    This pins the conversion contract: the exact POSIX number, which is why it
    skips on Windows.
    ``TestRustConsumeStream.test_propagates_io_errors`` in
    `test_rust_consume_stream.py` covers the consume entry point's own I/O-failure
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

    Parameters
    ----------
    rust_streams : ModuleType
        The compiled Rust streams extension module.
    directory_fd : int
        A descriptor open on a directory, which cannot be read.

    Notes
    -----
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

    Parameters
    ----------
    rust_streams : ModuleType
        The compiled Rust streams extension module.
    broken_pipe_fds : tuple[int, int]
        A closed read descriptor and an open write descriptor.

    Notes
    -----
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
