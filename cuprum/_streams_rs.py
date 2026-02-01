"""Optional Rust-backed stream operations for high-throughput operations.

This module provides a thin wrapper around the optional Rust extension. Import
or use it only when the Rust backend is installed.

Example
-------
bytes_written = rust_pump_stream(reader_fd, writer_fd, buffer_size=65536)
output = rust_consume_stream(reader_fd, buffer_size=65536)
"""

from __future__ import annotations

import functools
import importlib
import os
import typing as typ

if typ.TYPE_CHECKING:
    from types import ModuleType


@functools.lru_cache(maxsize=1)
def _load_native() -> ModuleType:
    """Import the native Rust backend module."""
    return importlib.import_module("cuprum._rust_backend_native")


def _convert_fd_for_platform(fd: int) -> int:
    """Convert a file descriptor for platform-specific Rust handling."""
    if os.name != "nt":
        return fd
    import ctypes
    import msvcrt

    # Use getattr to avoid cross-platform stub mismatches in type checking.
    get_osfhandle = typ.cast(
        "typ.Callable[[int], int]",
        getattr(msvcrt, "get_osfhandle"),  # noqa: B009  # https://github.com/leynos/cuprum/pull/29#discussion_r2743182508
    )
    handle = get_osfhandle(fd)
    bit_size = ctypes.sizeof(ctypes.c_void_p) * 8
    mask = (1 << bit_size) - 1
    return handle & mask


def rust_pump_stream(
    reader_fd: int,
    writer_fd: int,
    *,
    buffer_size: int = 65536,
) -> int:
    """Pump bytes between file descriptors using the Rust extension.

    Parameters
    ----------
    reader_fd : int
        File descriptor to read from.
    writer_fd : int
        File descriptor to write to.
    buffer_size : int, optional
        Buffer size in bytes for each read/write cycle. Must be greater than
        zero. Defaults to ``65536`` (64 KiB).

    Returns
    -------
    int
        The number of bytes successfully written.

    Raises
    ------
    ImportError
        If the Rust backend native module cannot be imported.
    ValueError
        If ``buffer_size`` is not a positive integer.
    OSError
        If an I/O error occurs while pumping bytes.
    """
    native = _load_native()
    return int(
        native.rust_pump_stream(
            _convert_fd_for_platform(reader_fd),
            _convert_fd_for_platform(writer_fd),
            buffer_size=buffer_size,
        ),
    )


def rust_consume_stream(
    reader_fd: int,
    *,
    buffer_size: int = 65536,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> str:
    """Consume bytes from a file descriptor using the Rust extension.

    Parameters
    ----------
    reader_fd : int
        File descriptor to read from.
    buffer_size : int, optional
        Buffer size in bytes for each read cycle. Must be greater than zero.
        Defaults to ``65536`` (64 KiB).
    encoding : str, optional
        Encoding name; only ``utf-8`` is supported by the Rust backend.
    errors : str, optional
        Error handling mode; only ``replace`` is supported by the Rust backend.

    Returns
    -------
    str
        Decoded output from the stream.

    Raises
    ------
    ImportError
        If the Rust backend native module cannot be imported.
    ValueError
        If unsupported encoding or error handling is requested.
    OSError
        If an I/O error occurs while reading.
    """
    native = _load_native()
    return typ.cast(
        "str",
        native.rust_consume_stream(
            _convert_fd_for_platform(reader_fd),
            buffer_size=buffer_size,
            encoding=encoding,
            errors=errors,
        ),
    )


__all__ = ["rust_consume_stream", "rust_pump_stream"]
