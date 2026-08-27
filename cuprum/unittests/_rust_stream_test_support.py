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
