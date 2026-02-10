"""Unit tests for Linux splice() optimization in the Rust stream pump.

These tests verify that the splice code path works correctly on Linux and
that fallback to read/write works for unsupported file descriptor types.

Example
-------
pytest cuprum/unittests/test_rust_splice.py
"""

from __future__ import annotations

import contextlib
import os
import threading
import typing as typ

from tests.helpers.stream_pipes import _pipe_pair, _read_all, _safe_close

if typ.TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType


def _pump_payload_threaded(
    streams: ModuleType,
    payload: bytes,
    *,
    buffer_size: int | None = None,
) -> tuple[bytes, int]:
    """Pump payload through the Rust stream using a writer thread to avoid deadlock."""
    with _pipe_pair() as (in_read, in_write, out_read, out_write):

        def writer() -> None:
            view = memoryview(payload)
            while view:
                written = os.write(in_write, view)
                view = view[written:]
            _safe_close(in_write)

        write_thread = threading.Thread(target=writer)
        write_thread.start()

        kwargs: dict[str, int] = {}
        if buffer_size is not None:
            kwargs["buffer_size"] = buffer_size
        transferred = streams.rust_pump_stream(in_read, out_write, **kwargs)
        _safe_close(out_write)

        output = _read_all(out_read)
        write_thread.join()

    return output, transferred


class TestSpliceOptimization:
    """Tests for Linux splice() optimisation and fallback behaviour."""

    @staticmethod
    def test_large_pipe_transfer(
        rust_streams: ModuleType,
    ) -> None:
        """Verify large pipe-to-pipe transfers complete correctly."""
        # 1 MB payload to exercise splice with multiple chunks
        payload = bytes(range(256)) * (1024 * 1024 // 256)

        output, transferred = _pump_payload_threaded(rust_streams, payload)

        assert output == payload, "expected large payload to round-trip"
        assert transferred == len(payload), "expected all bytes transferred"

    @staticmethod
    def test_file_to_pipe_fallback(
        rust_streams: ModuleType,
        tmp_path: Path,
    ) -> None:
        """Verify file-to-pipe transfers work using fallback."""
        payload = b"file-to-pipe-fallback-test-payload"
        test_file = tmp_path / "test_input.bin"
        test_file.write_bytes(payload)

        with contextlib.ExitStack() as stack:
            out_read, out_write = os.pipe()
            stack.callback(_safe_close, out_read)

            with test_file.open("rb") as f:
                transferred = rust_streams.rust_pump_stream(f.fileno(), out_write)
            _safe_close(out_write)
            output = _read_all(out_read)

        assert output == payload, "expected file content to transfer to pipe"
        assert transferred == len(payload), "expected all bytes transferred"

    @staticmethod
    def test_large_transfer_with_small_buffer(
        rust_streams: ModuleType,
    ) -> None:
        """Verify large transfers work with small buffer sizes."""
        # 256 KB payload with 4 KB buffer
        payload = bytes(range(256)) * 1024

        output, transferred = _pump_payload_threaded(
            rust_streams,
            payload,
            buffer_size=4096,
        )

        assert output == payload, "expected payload to round-trip with small buffer"
        assert transferred == len(payload), "expected all bytes transferred"

    @staticmethod
    def test_broken_pipe_drains_reader(
        rust_streams: ModuleType,
    ) -> None:
        """Verify BrokenPipe handling drains the reader to avoid upstream deadlock."""
        total_bytes = 1024 * 1024  # 1 MiB

        # Use _pipe_pair for resource management. The context manager creates:
        # - src_read_fd, src_write_fd: source pipe (writer thread -> rust_pump_stream)
        # - dst_read_fd, dst_write_fd: destination pipe (rust_pump_stream -> consumer)
        with _pipe_pair() as (src_read_fd, src_write_fd, dst_read_fd, dst_write_fd):

            def writer() -> None:
                with contextlib.ExitStack() as stack:
                    stack.callback(_safe_close, src_write_fd)
                    remaining = total_bytes
                    chunk = b"x" * 8192
                    while remaining > 0:
                        to_write = min(len(chunk), remaining)
                        os.write(src_write_fd, chunk[:to_write])
                        remaining -= to_write

            writer_thread = threading.Thread(target=writer)
            writer_thread.start()

            # Close the consumer's read end to trigger BrokenPipe on the writer.
            # _pipe_pair will attempt to close this again at exit, but _safe_close
            # handles double-close gracefully.
            _safe_close(dst_read_fd)

            # rust_pump_stream should:
            # - return promptly
            # - report the number of bytes actually written
            # - drain the remaining data from src_read_fd so the writer is not blocked
            bytes_pumped = rust_streams.rust_pump_stream(
                src_read_fd,
                dst_write_fd,
                buffer_size=64 * 1024,
            )

            writer_thread.join(timeout=2.0)
            assert not writer_thread.is_alive(), (
                "writer blocked because splice did not drain the reader"
            )

            assert 0 <= bytes_pumped < total_bytes, (
                "expected partial transfer count due to broken pipe"
            )
