"""Unit tests for the Rust stream pump."""

from __future__ import annotations

import contextlib
import errno
import os
import typing as typ

import pytest

from tests.helpers.stream_pipes import _pipe_pair, _read_all, _safe_close

if typ.TYPE_CHECKING:
    from types import ModuleType


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


def test_rust_pump_stream_transfers_bytes(rust_streams: ModuleType) -> None:
    """rust_pump_stream transfers bytes between pipes."""
    payload = b"cuprum-stream-payload"
    output, transferred = _pump_payload(rust_streams, payload)

    assert output == payload, "expected payload to round-trip through pump"
    assert transferred == len(payload), "expected transferred count to match payload"


def test_rust_pump_stream_respects_buffer_size(
    rust_streams: ModuleType,
) -> None:
    """rust_pump_stream honours the buffer_size parameter."""
    payload = os.urandom(16384)
    output, transferred = _pump_payload(rust_streams, payload, buffer_size=1024)

    assert output == payload, "expected payload to match output for custom buffer"
    assert transferred == len(payload), "expected transferred count to match payload"


def test_rust_pump_stream_raises_on_invalid_buffer(
    rust_streams: ModuleType,
) -> None:
    """rust_pump_stream rejects invalid buffer sizes."""
    with contextlib.ExitStack() as stack:
        in_read, in_write = os.pipe()
        stack.callback(_safe_close, in_read)
        stack.callback(_safe_close, in_write)
        with pytest.raises(ValueError, match="buffer_size"):
            rust_streams.rust_pump_stream(in_read, in_write, buffer_size=0)


def test_rust_pump_stream_propagates_io_errors(
    rust_streams: ModuleType,
) -> None:
    """rust_pump_stream raises OSError on I/O failure."""
    with contextlib.ExitStack() as stack:
        read_fd, write_fd = os.pipe()
        stack.callback(_safe_close, read_fd)
        stack.callback(_safe_close, write_fd)
        _safe_close(read_fd)
        with pytest.raises(
            OSError,
            match=r"(Bad file descriptor|invalid handle)",
        ) as excinfo:
            rust_streams.rust_pump_stream(read_fd, write_fd)
    assert excinfo.value.errno in {errno.EBADF, errno.EINVAL}, (
        "expected errno to indicate an invalid file descriptor/handle"
    )


def test_rust_pump_stream_transfers_zero_bytes(
    rust_streams: ModuleType,
) -> None:
    """rust_pump_stream handles empty input and returns zero."""
    payload = b""
    output, transferred = _pump_payload(rust_streams, payload)

    assert output == payload, "expected empty payload to remain empty"
    assert transferred == 0, "expected zero bytes transferred for empty payload"


def test_rust_pump_stream_ignores_broken_pipe(
    rust_streams: ModuleType,
) -> None:
    """rust_pump_stream drains input even if the writer breaks."""
    payload = b"x" * (64 * 1024)
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        _safe_close(out_read)
        os.write(in_write, payload)
        _safe_close(in_write)

        try:
            transferred = rust_streams.rust_pump_stream(in_read, out_write)
        except OSError as exc:
            if getattr(exc, "errno", None) in {errno.EPIPE, errno.ECONNRESET}:
                pytest.fail(
                    "rust_pump_stream raised OSError for broken pipe/connection reset"
                )
            raise

        remaining = os.read(in_read, 4096)
        assert remaining == b"", "expected input pipe to be fully drained"

    assert isinstance(transferred, int), "expected transfer count to be integer"
    assert transferred <= len(payload), "expected transfer count to be bounded"
