"""Shared descriptor helpers for wheel-packaged Rust stream tests."""

from __future__ import annotations

import contextlib
import errno
import os
import re

# An unusable descriptor reports `EBADF` on POSIX and `EINVAL` where Windows
# rejects the handle instead. The extension exposes the same condition with
# Win32's invalid-handle or invalid-parameter code in its rendered message.
INVALID_FD_ERRNOS = (errno.EBADF, errno.EINVAL)
INVALID_FD_WINERRORS = (6, 87)

# `pytest.raises(OSError)` needs a `match` to satisfy ruff PT011. Match the
# numeric prefix CPython formats itself, rather than locale-dependent strerror
# text, for both POSIX and Windows exception renderings.
INVALID_FD_MESSAGE_RE = "|".join(
    re.escape(f"[{prefix} {code}]")
    for prefix, codes in (
        ("Errno", INVALID_FD_ERRNOS),
        ("WinError", INVALID_FD_WINERRORS),
    )
    for code in codes
)


def _safe_close(fd: int) -> None:
    """Close a file descriptor, ignoring errors."""
    with contextlib.suppress(OSError):
        os.close(fd)


class _OwnedDescriptor:
    """Close one owned descriptor number at most once.

    A test must not close a number it no longer owns. Once the test, the
    feeder, or the native boundary has closed or adopted a descriptor, another
    thread can reuse that number, and a suppressed second close would then
    close an unrelated resource. Ownership is therefore tracked against the
    resource identity recorded when the descriptor was opened, not against
    whether the number happens to open: a reused number names something else.
    """

    def __init__(self, fd: int) -> None:
        """Take ownership of ``fd`` and record what it names."""
        self.fd = fd
        self._identity = _descriptor_identity(fd)
        self._is_owned = True

    def disarm(self) -> None:
        """Stop closing ``fd`` because another owner has it, or has closed it."""
        self._is_owned = False

    def disarm_if_adopted(self) -> None:
        """Disarm once ``fd`` no longer names the resource this owner opened.

        The native boundary closes what it adopts, so a closed number is the
        adoption signal. A number that was closed and then reused reports a
        different identity and disarms too, which stops the cleanup from
        closing a resource another thread now holds. A number that still names
        the owned resource means the call failed before adoption, so this
        owner keeps responsibility for closing it.
        """
        if _descriptor_identity(self.fd) != self._identity:
            self.disarm()

    def close(self) -> None:
        """Close ``fd`` once, and only while it still names the owned resource."""
        if not self._is_owned:
            return
        self._is_owned = False
        if _descriptor_identity(self.fd) == self._identity:
            _safe_close(self.fd)


def _descriptor_identity(fd: int) -> tuple[int, int, int] | None:
    """Return the resource identity behind ``fd``, or ``None`` when closed."""
    try:
        status = os.fstat(fd)
    except OSError:
        return None
    return (status.st_dev, status.st_ino, status.st_mode)


def _is_descriptor_open(fd: int) -> bool:
    """Return whether ``fd`` still names an open descriptor."""
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _descriptor_state(fd: int) -> str:
    """Return ``"open"`` or ``"closed"`` for a descriptor.

    ``os.fstat`` is the cheapest probe that distinguishes the two without
    disturbing the descriptor, which is what lets a test assert whether a
    writer was adopted.

    Returns
    -------
    str
        ``"open"`` when the descriptor is still usable, ``"closed"`` when it
        is not.
    """
    return "open" if _is_descriptor_open(fd) else "closed"


def _set_binary_mode(fd: int) -> None:
    """Keep pipe payload bytes unchanged when writing on Windows."""
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(fd, os.O_BINARY)
