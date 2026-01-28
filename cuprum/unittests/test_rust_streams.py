"""Unit tests for the Rust stream pump."""

from __future__ import annotations

import contextlib
import os
import typing as typ

import pytest

from cuprum import _rust_backend

if typ.TYPE_CHECKING:
    from types import ModuleType


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


def _load_streams_rs() -> ModuleType:
    """Import the Rust streams module or skip if unavailable."""
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed.")
    from cuprum import _streams_rs

    return _streams_rs


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


def test_rust_pump_stream_transfers_bytes() -> None:
    """rust_pump_stream transfers bytes between pipes."""
    streams = _load_streams_rs()
    payload = b"cuprum-stream-payload"
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        os.write(in_write, payload)
        _safe_close(in_write)

        transferred = streams.rust_pump_stream(in_read, out_write)

        _safe_close(out_write)
        output = _read_all(out_read)

    assert output == payload
    assert transferred == len(payload)


def test_rust_pump_stream_respects_buffer_size() -> None:
    """rust_pump_stream honours the buffer_size parameter."""
    streams = _load_streams_rs()
    payload = os.urandom(16384)
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        os.write(in_write, payload)
        _safe_close(in_write)

        transferred = streams.rust_pump_stream(
            in_read,
            out_write,
            buffer_size=1024,
        )

        _safe_close(out_write)
        output = _read_all(out_read)

    assert output == payload
    assert transferred == len(payload)


def test_rust_pump_stream_raises_on_invalid_buffer() -> None:
    """rust_pump_stream rejects invalid buffer sizes."""
    streams = _load_streams_rs()
    in_read, in_write = os.pipe()

    try:
        with pytest.raises(ValueError, match="buffer_size"):
            streams.rust_pump_stream(in_read, in_write, buffer_size=0)
    finally:
        _safe_close(in_read)
        _safe_close(in_write)


def test_rust_pump_stream_propagates_io_errors() -> None:
    """rust_pump_stream raises OSError on I/O failure."""
    streams = _load_streams_rs()
    read_fd, write_fd = os.pipe()

    try:
        _safe_close(read_fd)
        with pytest.raises(OSError, match=r".+"):
            streams.rust_pump_stream(read_fd, write_fd)
    finally:
        _safe_close(read_fd)
        _safe_close(write_fd)


def test_rust_pump_stream_ignores_broken_pipe() -> None:
    """rust_pump_stream drains input even if the writer breaks."""
    streams = _load_streams_rs()
    payload = b"broken-pipe"
    in_read, in_write = os.pipe()
    out_read, out_write = os.pipe()

    try:
        _safe_close(out_read)
        os.write(in_write, payload)
        _safe_close(in_write)

        transferred = streams.rust_pump_stream(in_read, out_write)
    finally:
        _safe_close(in_read)
        _safe_close(in_write)
        _safe_close(out_read)
        _safe_close(out_write)

    assert isinstance(transferred, int)
    assert transferred <= len(payload)
