"""Optional Rust-backed stream operations for high-throughput workloads.

This module provides a thin wrapper around the optional Rust extension. Import
or use it only when the Rust backend is installed.

Integration status: ``rust_pump_stream`` is wired into production through
``_pump_stream_dispatch`` (``cuprum/_pipeline_streams.py``).
``rust_consume_stream`` is **implemented but not yet integrated** — no
production code routes a consume through it; every consume currently uses the
pure-Python ``_consume_stream``. Consume-side dispatch is deliberately
deferred, evidence-gated work tracked as Phase 2 of ADR-002
(``docs/adr-002-additional-rust-components.md``).

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
    import collections.abc as cabc


class _NativeBackend(typ.Protocol):
    """Structural view of the optional ``cuprum._rust_backend_native`` module.

    Declaring the two entry points this wrapper uses gives them checked
    signatures and precise return types, so each call site needs no cast of
    its own.
    """

    @staticmethod
    def rust_pump_stream(
        reader_fd: int,
        writer_fd: int,
        *,
        buffer_size: int = ...,
    ) -> int:
        """Pump bytes from ``reader_fd`` to ``writer_fd``."""

    @staticmethod
    def rust_consume_stream(reader_fd: int, *, buffer_size: int = ...) -> str:
        """Drain ``reader_fd`` and decode it as UTF-8."""


@functools.lru_cache(maxsize=1)
def _load_native() -> _NativeBackend:
    """Import the native Rust backend module."""
    # The compiled extension ships no stubs, and ``import_module`` is typed as
    # returning a bare ``ModuleType`` whose ``__getattr__`` ty does not treat
    # as satisfying a protocol. One cast here is the whole untyped boundary;
    # every call through ``_NativeBackend`` is checked from this point on.
    return typ.cast(
        "_NativeBackend",
        importlib.import_module("cuprum._rust_backend_native"),
    )


def _convert_fd_for_platform(fd: int) -> int:
    """Convert a file descriptor for platform-specific Rust handling."""
    if os.name != "nt":
        return fd
    import ctypes
    import msvcrt

    # Use getattr to avoid cross-platform stub mismatches in type checking.
    get_osfhandle = typ.cast(
        "cabc.Callable[[int], int]",
        getattr(msvcrt, "get_osfhandle"),  # ruff: ignore[get-attr-with-constant]  # https://github.com/leynos/cuprum/pull/29#discussion_r2743182508
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
        zero and no larger than 1 GiB (``1 << 30``). Defaults to ``65536``
        (64 KiB).

    Returns
    -------
    int
        The number of bytes successfully written.

    Notes
    -----
    Failures propagate unchanged from the Rust extension rather than being
    raised here: ``ImportError`` if the native module cannot be imported,
    ``ValueError`` if ``buffer_size`` is not a positive integer or exceeds the
    1 GiB maximum, and ``OSError`` if an I/O error occurs while pumping bytes.
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
) -> str:
    """Consume bytes from a file descriptor using the Rust extension.

    This helper always decodes UTF-8 and replaces invalid sequences with the
    Unicode replacement character.

    Parameters
    ----------
    reader_fd : int
        File descriptor to read from.
    buffer_size : int, optional
        Buffer size in bytes for each read cycle. Must be greater than zero and
        no larger than 1 GiB (``1 << 30``). Defaults to ``65536`` (64 KiB).

    Returns
    -------
    str
        Decoded output from the stream.

    Notes
    -----
    Implemented but not yet integrated. No production code path routes a
    consume through this function; the pump side has a
    ``_pump_stream_dispatch`` counterpart, but consume dispatch is
    deferred, evidence-gated work (ADR-002, Phase 2). The function is
    exercised directly by tests and remains available for downstream
    experimentation.

    Failures propagate unchanged from the Rust extension rather than being
    raised here: ``ImportError`` if the native module cannot be imported,
    ``ValueError`` if ``buffer_size`` is not a positive integer or exceeds the
    1 GiB maximum, and ``OSError`` if an I/O error occurs while reading.
    """
    native = _load_native()
    return native.rust_consume_stream(
        _convert_fd_for_platform(reader_fd),
        buffer_size=buffer_size,
    )


__all__ = ["rust_consume_stream", "rust_pump_stream"]
