"""Unit tests for the Rust stream pump.

These tests validate the optional Rust-backed pump's transfer behaviour and
error handling. The consume entry point is covered in
``test_rust_consume_stream.py``.

Example
-------
pytest cuprum/unittests/test_rust_streams.py
"""

from __future__ import annotations

import contextlib
import errno
import os
import typing as typ

import pytest

from tests.helpers.stream_pipes import (
    INVALID_FD_ERRNOS,
    INVALID_FD_MESSAGE_RE,
    _pipe_pair,
    _pump_rust_stream_payload,
    _safe_close,
    feed_source_pipe,
)

if typ.TYPE_CHECKING:
    from types import ModuleType


@pytest.mark.parametrize(
    ("test_id", "payload", "buffer_size"),
    [
        ("basic_payload", b"cuprum-stream-payload", None),
        ("custom_buffer_size", bytes(range(256)) * 64, 1024),
        ("zero_bytes", b"", None),
    ],
    ids=["basic_payload", "custom_buffer_size", "zero_bytes"],
)
def test_rust_pump_stream_transfers_data(
    rust_streams: ModuleType,
    test_id: str,
    payload: bytes,
    buffer_size: int | None,
) -> None:
    """Validate rust_pump_stream transfers bytes between pipes.

    Parameterized test covering normal transfer, custom buffer sizes, and empty
    input.

    Parameters
    ----------
    rust_streams : ModuleType
        The Rust streams module fixture.
    test_id : str
        Test case identifier for parameterization.
    payload : bytes
        The payload to transfer through the pump.
    buffer_size : int | None
        Optional buffer size parameter; None uses default.

    Returns
    -------
    None
    """
    output, transferred = _pump_rust_stream_payload(
        rust_streams, payload, buffer_size=buffer_size
    )

    assert output == payload, f"expected payload to round-trip through pump ({test_id})"
    assert transferred == len(payload), (
        f"expected transferred count to match payload ({test_id})"
    )


def test_rust_pump_stream_raises_on_invalid_buffer(
    rust_streams: ModuleType,
) -> None:
    """Verify rust_pump_stream rejects invalid buffer sizes.

    Parameters
    ----------
    rust_streams : ModuleType
        The Rust streams module fixture.

    Returns
    -------
    None
    """
    with contextlib.ExitStack() as stack:
        in_read, in_write = os.pipe()
        stack.callback(_safe_close, in_read)
        stack.callback(_safe_close, in_write)
        with pytest.raises(ValueError, match="buffer_size"):
            rust_streams.rust_pump_stream(in_read, in_write, buffer_size=0)


def test_rust_pump_stream_propagates_io_errors(
    rust_streams: ModuleType,
) -> None:
    """Verify rust_pump_stream raises OSError on I/O failure.

    Parameters
    ----------
    rust_streams : ModuleType
        The Rust streams module fixture.

    Returns
    -------
    None
    """
    with contextlib.ExitStack() as stack:
        read_fd, write_fd = os.pipe()
        stack.callback(_safe_close, read_fd)
        stack.callback(_safe_close, write_fd)
        _safe_close(read_fd)
        with pytest.raises(
            OSError,
            match=INVALID_FD_MESSAGE_RE,
        ) as excinfo:
            rust_streams.rust_pump_stream(read_fd, write_fd)
    assert excinfo.value.errno in INVALID_FD_ERRNOS, (
        "expected errno to indicate an invalid file descriptor/handle, "
        f"found {excinfo.value.errno!r}"
    )


def test_rust_pump_stream_ignores_broken_pipe(
    rust_streams: ModuleType,
) -> None:
    """Verify rust_pump_stream drains input even if the writer breaks.

    Parameters
    ----------
    rust_streams : ModuleType
        The Rust streams module fixture.

    Returns
    -------
    None
    """
    payload = b"x" * (64 * 1024)
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        _safe_close(out_read)  # Break the destination before pumping.
        # Feed the source concurrently so the write cannot deadlock when the
        # payload exceeds the host pipe capacity.
        with feed_source_pipe(in_write, payload, cancel_fd=in_read):
            try:
                transferred = rust_streams.rust_pump_stream(in_read, out_write)
            except OSError as exc:
                if getattr(exc, "errno", None) in {errno.EPIPE, errno.ECONNRESET}:
                    pytest.fail(
                        "rust_pump_stream raised OSError for broken pipe/connection "
                        "reset"
                    )
                raise

            remaining = os.read(in_read, 4096)
            assert remaining == b"", "expected input pipe to be fully drained"

    assert isinstance(transferred, int), "expected transfer count to be integer"
    assert transferred <= len(payload), "expected transfer count to be bounded"
