"""Scoped synthetic descriptors for public native-pump behaviour tests."""

from __future__ import annotations

import asyncio
import contextlib
import os
import typing as typ

from cuprum import _backend, _pipeline_streams
from cuprum._testing import (
    configure_pump_stream_dispatch_for_testing,
    reset_pump_stream_dispatch_for_testing,
    set_rust_availability_for_testing,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest


@contextlib.contextmanager
def force_synthetic_native_pump_path(
    monkeypatch: pytest.MonkeyPatch,
) -> cabc.Iterator[None]:
    """Route a test-owned pipe pair through the otherwise unsupported native path."""
    reader_fd, writer_fd = os.pipe()

    def extract_raw_fd(
        stream: asyncio.StreamReader | asyncio.StreamWriter | None,
    ) -> int | None:
        """Return the pipe descriptor owned by the synthetic stream role."""
        match stream:
            case asyncio.StreamReader():
                return reader_fd
            case asyncio.StreamWriter():
                return writer_fd
            case _:
                return None

    with monkeypatch.context() as scoped_monkeypatch:
        scoped_monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
        scoped_monkeypatch.setattr(
            _pipeline_streams,
            "_native_pump_supported_on_platform",
            lambda: True,
        )
        configure_pump_stream_dispatch_for_testing(raw_fd_extractor=extract_raw_fd)
        set_rust_availability_for_testing(is_available=True)
        _backend._check_rust_available.cache_clear()
        _backend.get_stream_backend.cache_clear()
        try:
            yield
        finally:
            reset_pump_stream_dispatch_for_testing()
            set_rust_availability_for_testing(is_available=None)
            _backend._check_rust_available.cache_clear()
            _backend.get_stream_backend.cache_clear()
            for fd in (reader_fd, writer_fd):
                with contextlib.suppress(OSError):
                    os.close(fd)
