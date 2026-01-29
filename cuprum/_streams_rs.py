"""Rust-backed stream operations (optional)."""

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
    import msvcrt

    # Use getattr to avoid cross-platform stub mismatches in type checking.
    get_osfhandle = typ.cast(
        "typ.Callable[[int], int]",
        getattr(msvcrt, "get_osfhandle"),  # noqa: B009
    )
    return get_osfhandle(fd)


def rust_pump_stream(
    reader_fd: int,
    writer_fd: int,
    *,
    buffer_size: int = 65536,
) -> int:
    """Pump bytes between file descriptors using the Rust extension."""
    native = _load_native()
    return int(
        native.rust_pump_stream(
            _convert_fd_for_platform(reader_fd),
            _convert_fd_for_platform(writer_fd),
            buffer_size=buffer_size,
        ),
    )


__all__ = ["rust_pump_stream"]
