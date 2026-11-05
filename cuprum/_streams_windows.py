"""Windows descriptor and handle adapters for native stream calls."""

from __future__ import annotations

import contextlib
import os
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType


# ``DuplicateHandle`` takes a BOOL and a flag word in its last two slots.
# ctypes binds foreign calls positionally only, so keyword arguments would
# fail at runtime; these names keep each literal out of the argument list.
_DO_NOT_INHERIT_HANDLE = 0
_DUPLICATE_SAME_ACCESS = 2


def _kernel32_handle_api() -> ModuleType:
    """Load Kernel32 with the handle signatures required by the pump."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # ty: ignore[unresolved-attribute]  # Windows-only ctypes API.
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.DuplicateHandle.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    )
    kernel32.DuplicateHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def _windows_error(ctypes_module: ModuleType) -> OSError:
    """Build an OSError from a Windows-only ctypes last-error function."""
    get_last_error = typ.cast(
        "cabc.Callable[[], int]",
        getattr(ctypes_module, "get_last_error"),  # ruff: ignore[get-attr-with-constant]  # Windows-only API.
    )
    win_error = typ.cast(
        "cabc.Callable[[int], OSError]",
        getattr(ctypes_module, "WinError"),  # ruff: ignore[get-attr-with-constant]  # Windows-only API.
    )
    return win_error(get_last_error())


def _duplicate_windows_handle(handle: int) -> int:
    """Duplicate a Win32 handle so Rust can own it independently of the CRT."""
    import ctypes

    kernel32 = _kernel32_handle_api()
    current_process = kernel32.GetCurrentProcess()
    duplicated_handle = ctypes.c_void_p()
    duplicate_succeeded = kernel32.DuplicateHandle(
        current_process,
        ctypes.c_void_p(handle),
        current_process,
        ctypes.byref(duplicated_handle),
        0,
        _DO_NOT_INHERIT_HANDLE,
        _DUPLICATE_SAME_ACCESS,
    )
    if not duplicate_succeeded:
        raise _windows_error(ctypes)
    if duplicated_handle.value is None:
        msg = "DuplicateHandle returned a null handle"
        raise OSError(msg)
    return int(duplicated_handle.value)


def _close_windows_handle(handle: int) -> None:
    """Close a duplicated Win32 handle that was not handed to Rust."""
    import ctypes

    if not _kernel32_handle_api().CloseHandle(ctypes.c_void_p(handle)):
        raise _windows_error(ctypes)


@contextlib.contextmanager
def _duplicate_binary_reader(reader_fd: int) -> cabc.Iterator[int]:
    """Yield a duplicated CRT descriptor in binary mode, then close it."""
    duplicate_fd = os.dup(reader_fd)
    try:
        import msvcrt

        setmode = typ.cast(
            "cabc.Callable[[int, int], int]",
            getattr(msvcrt, "setmode"),  # ruff: ignore[get-attr-with-constant]  # Windows-only API.
        )
        binary_mode: int = os.O_BINARY  # ty: ignore[unresolved-attribute]  # Windows-only constant.
        setmode(duplicate_fd, binary_mode)
        yield duplicate_fd
    finally:
        os.close(duplicate_fd)


def _transfer_writer_fd_for_platform(writer_fd: int) -> int:
    """Return a writer resource that Rust can consume without a CRT FD leak."""
    if os.name != "nt":
        return writer_fd

    owned_handle = _duplicate_windows_handle(_convert_fd_for_platform(writer_fd))
    try:
        os.close(writer_fd)
    except BaseException:
        _close_windows_handle(owned_handle)
        raise
    return owned_handle


def _convert_fd_for_platform(fd: int) -> int:
    """Return the native value expected by the platform stream boundary."""
    if os.name != "nt":
        return fd

    import ctypes
    import msvcrt

    get_osfhandle = typ.cast(
        "cabc.Callable[[int], int]",
        getattr(msvcrt, "get_osfhandle"),  # ruff: ignore[get-attr-with-constant]  # Windows-only API.
    )
    handle = get_osfhandle(fd)
    bit_size = ctypes.sizeof(ctypes.c_void_p) * 8
    mask = (1 << bit_size) - 1
    return handle & mask
