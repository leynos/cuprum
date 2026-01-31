"""Internal helpers for stream pipe tests."""

from __future__ import annotations

import contextlib
import os
import typing as typ


def _safe_close(fd: int) -> None:
    """Close a file descriptor, ignoring errors."""
    with contextlib.suppress(OSError):
        os.close(fd)


def _read_all(fd: int, *, chunk_size: int = 4096) -> bytes:
    """Read all data from a file descriptor until EOF."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, chunk_size)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


@contextlib.contextmanager
def _pipe_pair() -> typ.Iterator[tuple[int, int, int, int]]:
    """Manage pipe creation and cleanup for stream tests."""
    in_read, in_write = os.pipe()
    out_read, out_write = os.pipe()
    try:
        yield in_read, in_write, out_read, out_write
    finally:
        _safe_close(in_read)
        _safe_close(in_write)
        _safe_close(out_read)
        _safe_close(out_write)
