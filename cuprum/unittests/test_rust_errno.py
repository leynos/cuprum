"""`OSError`s crossing the PyO3 boundary carry a usable `errno`.

An extension that reports failures only in message text forces callers to parse
English to tell one failure from another, and message text is not a stable
interface. These tests pin the two properties that make the error branchable:
`errno` is populated, and the exception is the subclass that errno implies.

They isolate the conversion rather than reaching it through a pump: the pump
treats a broken pipe as non-fatal and drains, so most interesting errnos never
propagate through it. Calling the exported entry points with a deliberately
unusable descriptor reaches the conversion directly.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType

# These cases name POSIX errnos and the subclasses CPython derives from them.
# Windows reaches the same conversion but carries a Win32 code through
# `winerror`, from which CPython derives a different errno, so the expected
# numbers here do not hold there. Skip rather than encode both taxonomies:
# the Windows arm has no test job to run in.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX errno values and the subclasses derived from them",
)


@pytest.fixture(name="broken_pipe_fds")
def fixture_broken_pipe_fds() -> cabc.Iterator[tuple[int, int]]:
    """Yield ``(closed_read_fd, open_write_fd)`` for a pipe.

    The read end is closed before the test runs, so any attempt to read it
    fails with ``EBADF`` while the write end stays valid.
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

    with pytest.raises(OSError, match="Bad file descriptor") as excinfo:
        rust_streams.rust_pump_stream(closed_read_fd, write_fd)

    assert excinfo.value.errno == errno.EBADF, (
        f"a closed descriptor must report EBADF in errno, found {excinfo.value.errno!r}"
    )
    assert excinfo.value.strerror, (
        "strerror must be populated alongside errno, so the pair reads as a "
        "normal OSError"
    )


def test_consume_error_reports_a_branchable_errno(
    rust_streams: ModuleType,
    broken_pipe_fds: tuple[int, int],
) -> None:
    """The consume entry point preserves `errno` on the same terms."""
    closed_read_fd, _write_fd = broken_pipe_fds

    with pytest.raises(OSError, match="Bad file descriptor") as excinfo:
        rust_streams.rust_consume_stream(closed_read_fd)

    assert excinfo.value.errno == errno.EBADF, (
        f"expected EBADF, found {excinfo.value.errno!r}"
    )


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

    with pytest.raises(OSError, match="Bad file descriptor") as excinfo:
        rust_streams.rust_consume_stream(closed_read_fd)

    assert "os error" not in str(excinfo.value), (
        "the Rust-side suffix must be stripped, leaving Python's own "
        f"[Errno N] prefix as the only mention; found {str(excinfo.value)!r}"
    )
    assert str(excinfo.value).startswith(f"[Errno {errno.EBADF}]"), (
        f"expected a normal OSError rendering, found {str(excinfo.value)!r}"
    )
