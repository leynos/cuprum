"""Unit tests for the Rust stream pump."""

from __future__ import annotations

import errno
import os
import typing as typ

import pytest

from cuprum import _rust_backend
from tests.helpers.stream_pipes import pipe_pair as _pipe_pair
from tests.helpers.stream_pipes import read_all as _read_all
from tests.helpers.stream_pipes import safe_close as _safe_close

if typ.TYPE_CHECKING:
    from types import ModuleType


def _load_streams_rs() -> ModuleType:
    """Import the Rust streams module or skip if unavailable."""
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed.")
    from cuprum import _streams_rs

    return _streams_rs


def _pump_payload(
    streams: ModuleType,
    payload: bytes,
    *,
    buffer_size: int | None = None,
) -> tuple[bytes, int]:
    """Pump payload through the Rust stream and return output and count."""
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        os.write(in_write, payload)
        _safe_close(in_write)

        if buffer_size is None:
            transferred = streams.rust_pump_stream(in_read, out_write)
        else:
            transferred = streams.rust_pump_stream(
                in_read,
                out_write,
                buffer_size=buffer_size,
            )

        _safe_close(out_write)
        output = _read_all(out_read)

    return output, transferred


def test_rust_pump_stream_transfers_bytes() -> None:
    """rust_pump_stream transfers bytes between pipes."""
    streams = _load_streams_rs()
    payload = b"cuprum-stream-payload"
    output, transferred = _pump_payload(streams, payload)

    assert output == payload
    assert transferred == len(payload)


def test_rust_pump_stream_respects_buffer_size() -> None:
    """rust_pump_stream honours the buffer_size parameter."""
    streams = _load_streams_rs()
    payload = os.urandom(16384)
    output, transferred = _pump_payload(streams, payload, buffer_size=1024)

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


def test_rust_pump_stream_transfers_zero_bytes() -> None:
    """rust_pump_stream handles empty input and returns zero."""
    streams = _load_streams_rs()
    payload = b""
    output, transferred = _pump_payload(streams, payload)

    assert output == payload
    assert transferred == 0


def test_rust_pump_stream_ignores_broken_pipe() -> None:
    """rust_pump_stream drains input even if the writer breaks."""
    streams = _load_streams_rs()
    payload = b"x" * (64 * 1024)
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        _safe_close(out_read)
        os.write(in_write, payload)
        _safe_close(in_write)

        try:
            transferred = streams.rust_pump_stream(in_read, out_write)
        except OSError as exc:
            if getattr(exc, "errno", None) in {errno.EPIPE, errno.ECONNRESET}:
                pytest.fail(
                    "rust_pump_stream raised OSError for broken pipe/connection reset"
                )
            raise

        remaining = os.read(in_read, 4096)
        assert remaining == b""

    assert isinstance(transferred, int)
    assert transferred <= len(payload)
