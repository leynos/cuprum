"""Ownership tests for the Python-to-Rust writer hand-off."""

from __future__ import annotations

import ctypes
import errno
import typing as typ
from types import ModuleType
from unittest import mock

import pytest

from cuprum import _streams_rs

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _NativePumpModule(ModuleType):
    """Module double exposing the native pump callable."""

    rust_pump_stream: cabc.Callable[..., int]


def _native_pump_module(native_pump: cabc.Callable[..., int]) -> ModuleType:
    """Return a native-module double exposing ``native_pump``."""
    native_module = _NativePumpModule("rust_pump_double")
    native_module.rust_pump_stream = native_pump
    return native_module


def _patch_windows_pump(
    monkeypatch: pytest.MonkeyPatch,
    *,
    native_pump: cabc.Callable[..., int],
    kernel32: mock.Mock,
    closed_fds: list[int],
) -> None:
    """Install platform-neutral Windows and native pump doubles."""
    reader_fd = 11
    writer_fd = 12
    reader_handle = 101
    writer_handle = 102

    def convert_fd(fd: int) -> int:
        """Map the simulated CRT descriptors to their Win32 handles."""
        return {reader_fd: reader_handle, writer_fd: writer_handle}[fd]

    monkeypatch.setattr(_streams_rs.os, "name", "nt")
    monkeypatch.setattr(
        _streams_rs,
        "_load_native",
        lambda: _native_pump_module(native_pump),
    )
    monkeypatch.setattr(_streams_rs, "_convert_fd_for_platform", convert_fd)
    monkeypatch.setattr(
        ctypes, "WinDLL", mock.Mock(return_value=kernel32), raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: errno.EIO, raising=False)
    monkeypatch.setattr(
        ctypes,
        "WinError",
        lambda error: OSError(error, "simulated Windows error"),
        raising=False,
    )
    monkeypatch.setattr(_streams_rs.os, "close", closed_fds.append)


def test_windows_writer_transfer_releases_the_crt_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rust receives an owned handle after Python releases the CRT duplicate."""
    reader_fd = 11
    writer_fd = 12
    reader_handle = 101
    rust_owned_handle = 103
    native_pump = mock.Mock(return_value=7)
    closed_fds: list[int] = []
    kernel32 = mock.Mock()
    _patch_windows_pump(
        monkeypatch,
        native_pump=native_pump,
        kernel32=kernel32,
        closed_fds=closed_fds,
    )
    monkeypatch.setattr(
        _streams_rs,
        "_duplicate_windows_handle",
        lambda _handle: rust_owned_handle,
    )

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


def test_windows_writer_transfer_closes_crt_fd_when_duplicate_handle_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Win32 duplication leaves Python to close the CRT writer."""
    writer_fd = 12
    native_pump = mock.Mock(return_value=7)
    closed_fds: list[int] = []
    kernel32 = mock.Mock()
    kernel32.GetCurrentProcess.return_value = 1
    kernel32.DuplicateHandle.return_value = False
    _patch_windows_pump(
        monkeypatch,
        native_pump=native_pump,
        kernel32=kernel32,
        closed_fds=closed_fds,
    )

    with pytest.raises(OSError, match="simulated Windows error"):
        _streams_rs.rust_pump_stream(11, writer_fd)

    assert closed_fds == [writer_fd]
    native_pump.assert_not_called()


def test_windows_writer_transfer_closes_crt_fd_when_duplicate_handle_is_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A null Win32 duplication result leaves Python to close the CRT writer."""
    writer_fd = 12
    native_pump = mock.Mock(return_value=7)
    closed_fds: list[int] = []
    kernel32 = mock.Mock()
    kernel32.GetCurrentProcess.return_value = 1
    kernel32.DuplicateHandle.return_value = True
    _patch_windows_pump(
        monkeypatch,
        native_pump=native_pump,
        kernel32=kernel32,
        closed_fds=closed_fds,
    )

    with pytest.raises(OSError, match="null handle"):
        _streams_rs.rust_pump_stream(11, writer_fd)

    assert closed_fds == [writer_fd]
    native_pump.assert_not_called()


def test_windows_writer_transfer_closes_handle_when_crt_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed CRT close releases the separate Win32 handle before retrying."""
    writer_fd = 12
    rust_owned_handle = 103
    native_pump = mock.Mock(return_value=7)
    close_attempts: list[int] = []
    kernel32 = mock.Mock()
    kernel32.GetCurrentProcess.return_value = 1
    kernel32.CloseHandle.return_value = True

    def duplicate_handle(*arguments: object) -> bool:
        """Return the configured owned Win32 handle through the out parameter."""
        destination = typ.cast("ctypes.c_void_p", arguments[3])
        ctypes.cast(destination, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(
            rust_owned_handle
        )
        return True

    def close_crt_fd(fd: int) -> None:
        """Fail once, then let the outer pre-native cleanup close the CRT FD."""
        close_attempts.append(fd)
        if len(close_attempts) == 1:
            raise OSError(errno.EIO, "simulated CRT close error")

    kernel32.DuplicateHandle.side_effect = duplicate_handle
    _patch_windows_pump(
        monkeypatch,
        native_pump=native_pump,
        kernel32=kernel32,
        closed_fds=close_attempts,
    )
    monkeypatch.setattr(_streams_rs.os, "close", close_crt_fd)

    with pytest.raises(OSError, match="simulated CRT close error"):
        _streams_rs.rust_pump_stream(11, writer_fd)

    assert close_attempts == [writer_fd, writer_fd]
    assert kernel32.CloseHandle.call_args.args[0].value == rust_owned_handle
    native_pump.assert_not_called()


@pytest.mark.parametrize("buffer_size", [0, (1 << 30) + 1])
def test_windows_invalid_buffer_does_not_transfer_writer_resource(
    monkeypatch: pytest.MonkeyPatch,
    buffer_size: int,
) -> None:
    """Invalid buffers must fail before the writer becomes a Win32 handle."""
    native_pump = mock.Mock(return_value=7)
    closed_fds: list[int] = []
    kernel32 = mock.Mock()
    _patch_windows_pump(
        monkeypatch,
        native_pump=native_pump,
        kernel32=kernel32,
        closed_fds=closed_fds,
    )

    with pytest.raises(ValueError, match="buffer_size"):
        _streams_rs.rust_pump_stream(11, 12, buffer_size=buffer_size)

    kernel32.DuplicateHandle.assert_not_called()
    assert closed_fds == [12]
    native_pump.assert_not_called()
