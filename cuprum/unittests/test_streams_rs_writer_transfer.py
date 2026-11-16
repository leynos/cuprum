"""Ownership tests for the Python-to-Rust writer hand-off."""

from __future__ import annotations

import errno
import sys
import typing as typ
from types import ModuleType
from unittest import mock

import pytest

from cuprum import _streams_rs, _streams_windows

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _NativePumpModule(ModuleType):
    """Module double exposing the native pump callable."""

    rust_pump_stream: cabc.Callable[..., int]


class _OneShotBufferSize:
    """Return a valid index once, then fail if conversion is repeated."""

    def __init__(self, value: int) -> None:
        """Store the sole value ``__index__`` may return."""
        self.calls = 0
        self.value = value

    def __index__(self) -> int:
        """Return the stored value and reject repeat conversion."""
        self.calls += 1
        if self.calls > 1:
            msg = "buffer size was converted after writer hand-off"
            raise RuntimeError(msg)
        return self.value


def _native_pump_module(native_pump: cabc.Callable[..., int]) -> ModuleType:
    """Return a native-module double exposing ``native_pump``."""
    native_module = _NativePumpModule("rust_pump_double")
    native_module.rust_pump_stream = native_pump
    return native_module


def _patch_windows_pump(
    monkeypatch: pytest.MonkeyPatch,
    *,
    native_pump: cabc.Callable[..., int],
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
    # The Windows adapters live in their own module, so patch each name in the
    # namespace that actually resolves it. Patching the re-export in
    # ``_streams_rs`` would leave the real implementation running.
    monkeypatch.setattr(_streams_windows, "_convert_fd_for_platform", convert_fd)
    monkeypatch.setattr(_streams_rs, "_convert_fd_for_platform", convert_fd)
    # Both modules hold the same ``os`` object, so this one patch covers the
    # adapter's close and the caller's pre-native cleanup alike.
    monkeypatch.setattr(_streams_rs.os, "close", closed_fds.append)


class _WinapiModule(ModuleType):
    """Module double declaring the three ``_winapi`` handle entry points."""

    GetCurrentProcess: mock.Mock
    DuplicateHandle: mock.Mock
    CloseHandle: mock.Mock


def _winapi_double(*, duplicate: cabc.Callable[..., int]) -> _WinapiModule:
    """Return a stand-in for CPython's Windows-only ``_winapi`` module."""
    winapi = _WinapiModule("_winapi")
    winapi.GetCurrentProcess = mock.Mock(return_value=1)
    winapi.DuplicateHandle = mock.Mock(side_effect=duplicate)
    winapi.CloseHandle = mock.Mock(return_value=None)
    return winapi


def _duplicate_returning(handle: int) -> cabc.Callable[..., int]:
    """Return a ``DuplicateHandle`` replacement that yields ``handle``."""

    def duplicate(*_arguments: object) -> int:
        """Ignore the request and report the configured handle."""
        return handle

    return duplicate


def test_duplicate_windows_handle_returns_a_non_inheritable_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate is taken from the current process and is not inherited."""
    winapi = _winapi_double(duplicate=_duplicate_returning(103))
    monkeypatch.setitem(sys.modules, "_winapi", winapi)

    duplicated = _streams_windows._duplicate_windows_handle(12)

    assert duplicated == 103, "expected the duplicated handle number to be returned"
    # Positional order is part of the contract under test: ``_winapi`` binds
    # these arguments positionally, so the flag must sit in the inherit slot.
    winapi.DuplicateHandle.assert_called_once_with(
        1,
        12,
        1,
        0,
        False,  # ruff: ignore[boolean-positional-value-in-call] - position under test
        _streams_windows._DUPLICATE_SAME_ACCESS,
    )


def test_duplicate_windows_handle_rejects_a_null_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A null duplicate is an error, not a handle Rust may own."""
    monkeypatch.setitem(
        sys.modules, "_winapi", _winapi_double(duplicate=_duplicate_returning(0))
    )

    with pytest.raises(OSError, match="null handle"):
        _streams_windows._duplicate_windows_handle(12)


def test_duplicate_windows_handle_propagates_the_win32_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused duplication surfaces ``_winapi``'s own error, unwrapped."""

    def refuse(*_arguments: object) -> int:
        """Fail the way ``_winapi.DuplicateHandle`` reports a Win32 error."""
        message = "simulated Windows error"
        raise OSError(errno.EIO, message)

    monkeypatch.setitem(sys.modules, "_winapi", _winapi_double(duplicate=refuse))

    with pytest.raises(OSError, match="simulated Windows error"):
        _streams_windows._duplicate_windows_handle(12)


def test_close_windows_handle_propagates_the_win32_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused close surfaces ``_winapi``'s own error, unwrapped."""
    winapi = _winapi_double(duplicate=_duplicate_returning(103))
    winapi.CloseHandle.side_effect = OSError(errno.EIO, "simulated close error")
    monkeypatch.setitem(sys.modules, "_winapi", winapi)

    with pytest.raises(OSError, match="simulated close error"):
        _streams_windows._close_windows_handle(103)


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
    _patch_windows_pump(
        monkeypatch,
        native_pump=native_pump,
        closed_fds=closed_fds,
    )
    monkeypatch.setattr(
        _streams_windows,
        "_duplicate_windows_handle",
        lambda _handle: rust_owned_handle,
    )

    result = _streams_rs.rust_pump_stream(reader_fd, writer_fd)

    assert result == 7, "expected the native pump result to reach its caller"
    assert closed_fds == [writer_fd], (
        "the duplicated CRT descriptor must be released before Rust owns its handle"
    )
    native_pump.assert_called_once_with(
        reader_handle,
        rust_owned_handle,
        buffer_size=65536,
    )


def test_windows_writer_transfer_passes_the_normalized_buffer_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stateful ``__index__`` runs before the writer handle is transferred."""
    reader_fd = 11
    writer_fd = 12
    reader_handle = 101
    rust_owned_handle = 103
    native_pump = mock.Mock(return_value=7)
    closed_fds: list[int] = []
    buffer_size = _OneShotBufferSize(1024)
    _patch_windows_pump(
        monkeypatch,
        native_pump=native_pump,
        closed_fds=closed_fds,
    )
    monkeypatch.setattr(
        _streams_windows,
        "_duplicate_windows_handle",
        lambda _handle: rust_owned_handle,
    )

    result = _streams_rs.rust_pump_stream(
        reader_fd,
        writer_fd,
        buffer_size=typ.cast("int", buffer_size),
    )

    assert result == 7, "expected the native pump result to reach its caller"
    assert buffer_size.calls == 1, (
        "expected buffer-size conversion before the writer hand-off"
    )
    assert closed_fds == [writer_fd], (
        "expected Python to release the CRT duplicate after successful transfer"
    )
    native_pump.assert_called_once_with(
        reader_handle,
        rust_owned_handle,
        buffer_size=1024,
    )


def _refuse_duplication(*_arguments: object) -> int:
    """Fail the way ``_winapi.DuplicateHandle`` reports a Win32 error."""
    message = "simulated Windows error"
    raise OSError(errno.EIO, message)


def test_windows_writer_transfer_closes_crt_fd_when_duplicate_handle_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid Win32 duplication leaves Python to close the CRT writer."""
    writer_fd = 12
    native_pump = mock.Mock(return_value=7)
    closed_fds: list[int] = []
    _patch_windows_pump(
        monkeypatch,
        native_pump=native_pump,
        closed_fds=closed_fds,
    )
    monkeypatch.setitem(
        sys.modules, "_winapi", _winapi_double(duplicate=_refuse_duplication)
    )

    with pytest.raises(OSError, match="simulated Windows error"):
        _streams_rs.rust_pump_stream(11, writer_fd)

    assert closed_fds == [writer_fd], (
        "expected Python to close the CRT duplicate after invalid Win32 transfer"
    )
    native_pump.assert_not_called()


def test_windows_writer_transfer_closes_handle_when_crt_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed CRT close releases the separate Win32 handle before retrying."""
    writer_fd = 12
    rust_owned_handle = 103
    native_pump = mock.Mock(return_value=7)
    close_attempts: list[int] = []
    winapi = _winapi_double(duplicate=_duplicate_returning(rust_owned_handle))

    def close_crt_fd(fd: int) -> None:
        """Fail once, then let the outer pre-native cleanup close the CRT FD."""
        close_attempts.append(fd)
        if len(close_attempts) == 1:
            raise OSError(errno.EIO, "simulated CRT close error")

    _patch_windows_pump(
        monkeypatch,
        native_pump=native_pump,
        closed_fds=close_attempts,
    )
    monkeypatch.setitem(sys.modules, "_winapi", winapi)
    monkeypatch.setattr(_streams_rs.os, "close", close_crt_fd)

    with pytest.raises(OSError, match="simulated CRT close error"):
        _streams_rs.rust_pump_stream(11, writer_fd)

    assert close_attempts == [writer_fd, writer_fd], (
        "expected the CRT writer close to be retried by pre-native cleanup"
    )
    winapi.CloseHandle.assert_called_once_with(rust_owned_handle)
    native_pump.assert_not_called()


@pytest.mark.parametrize("buffer_size", [0, (1 << 30) + 1])
def test_windows_invalid_buffer_does_not_transfer_writer_resource(
    monkeypatch: pytest.MonkeyPatch,
    buffer_size: int,
) -> None:
    """Invalid buffers must fail before the writer becomes a Win32 handle."""
    native_pump = mock.Mock(return_value=7)
    closed_fds: list[int] = []
    winapi = _winapi_double(duplicate=_duplicate_returning(103))
    _patch_windows_pump(
        monkeypatch,
        native_pump=native_pump,
        closed_fds=closed_fds,
    )
    monkeypatch.setitem(sys.modules, "_winapi", winapi)

    with pytest.raises(ValueError, match="buffer_size"):
        _streams_rs.rust_pump_stream(11, 12, buffer_size=buffer_size)

    winapi.DuplicateHandle.assert_not_called()
    assert closed_fds == [12], (
        "expected pre-native validation to close the still Python-owned writer"
    )
    native_pump.assert_not_called()
