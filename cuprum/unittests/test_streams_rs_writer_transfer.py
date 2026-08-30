"""Ownership tests for the Python-to-Rust writer hand-off."""

from __future__ import annotations

import typing as typ
from types import ModuleType
from unittest import mock

from cuprum import _streams_rs

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest


class _NativePumpModule(ModuleType):
    """Module double exposing the native pump callable."""

    rust_pump_stream: cabc.Callable[..., int]


def test_windows_writer_transfer_releases_the_crt_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rust receives an owned handle after Python releases the CRT duplicate."""
    reader_fd = 11
    writer_fd = 12
    reader_handle = 101
    writer_handle = 102
    rust_owned_handle = 103
    native_pump = mock.Mock(return_value=7)
    native_module = _NativePumpModule("rust_pump_double")
    native_module.rust_pump_stream = native_pump
    closed_fds: list[int] = []

    def convert_fd(fd: int) -> int:
        """Map the simulated CRT descriptors to their Win32 handles."""
        return {reader_fd: reader_handle, writer_fd: writer_handle}[fd]

    monkeypatch.setattr(_streams_rs.os, "name", "nt")
    monkeypatch.setattr(_streams_rs, "_load_native", lambda: native_module)
    monkeypatch.setattr(_streams_rs, "_convert_fd_for_platform", convert_fd)
    monkeypatch.setattr(
        _streams_rs,
        "_duplicate_windows_handle",
        lambda _handle: rust_owned_handle,
    )
    monkeypatch.setattr(_streams_rs.os, "close", closed_fds.append)

    result = _streams_rs.rust_pump_stream(reader_fd, writer_fd)

    assert result == 7
    assert closed_fds == [writer_fd], (
        "the duplicated CRT descriptor must be released before Rust owns its handle"
    )
    native_pump.assert_called_once_with(
        reader_handle,
        rust_owned_handle,
        buffer_size=65536,
    )
