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

    Ownership is decided by the contract of the call the descriptor is handed
    to, never by inspecting the number: descriptor metadata cannot distinguish
    an owned descriptor from a number another thread has reused, and on
    Windows pipe metadata is zeroed entirely. A caller that transfers
    ownership calls :meth:`disarm` as part of that transfer; a caller that
    keeps it leaves the owner armed so the cleanup closes it exactly once.
    """

    def __init__(self, fd: int) -> None:
        """Take ownership of ``fd``."""
        self.fd = fd
        self._is_owned = True

    def disarm(self) -> None:
        """Stop closing ``fd`` because ownership has transferred or ended."""
        self._is_owned = False

    def close(self) -> None:
        """Close ``fd`` once if this owner still holds it."""
        if self._is_owned:
            self._is_owned = False
            _safe_close(self.fd)


def _open_pipe_sink(stack: contextlib.ExitStack) -> _OwnedDescriptor:
    """Open a pipe write end for a native pump, keeping its read end open.

    The read end stays open so the pump's writes cannot block and the stack
    closes both ends. The owner is disarmed where the native entry adopts the
    writer, and left armed where the call rejects it first.

    Returns
    -------
    _OwnedDescriptor
        The write end, owned until the pump adopts it or the stack closes it.
    """
    reader_fd, writer_fd = os.pipe()
    stack.callback(_safe_close, reader_fd)
    owner = _OwnedDescriptor(writer_fd)
    # Register here so a caller that forgets cannot leak the write end;
    # ``close`` is idempotent, so disarming still suppresses the close.
    stack.callback(owner.close)
    return owner


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
