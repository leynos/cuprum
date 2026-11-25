"""Windows descriptor and handle adapters for native stream calls.

Windows is the one platform where a Python descriptor cannot be handed to the
extension's stream entry points unchanged, so the translation lives here rather
than in the shared wrapper:

- ``cuprum/_streams_rs.py`` imports this module for its native pump call, which
  transfers a writer handle, and for its native consume call, which transfers a
  CRT descriptor. It also re-exports the helpers so the parity harness in
  ``tests/helpers/consume_boundary_oracle.py`` can reach the same code path the
  wrapper uses rather than reimplementing it.
- ``rust_pump_stream`` receives an owned, duplicated Win32 handle;
  ``_duplicate_windows_handle`` and ``_close_windows_handle`` are what make that
  transfer possible, and ``_transfer_writer_fd_for_platform`` is the single
  place a CRT descriptor is converted into one.
- ``rust_consume_stream`` gets a duplicated binary-mode reader from
  ``_duplicate_binary_reader``, which closes it again after the call. The
  native boundary takes a further duplicate of its own, so a direct caller that
  skips this wrapper is equally safe; see
  ``rust/cuprum-native-io/src/windows_crt.rs``. Both duplicates exist for
  different reasons — this one so the caller's translation mode is untouched
  before the GIL is released, the native one so the detached read cannot be
  raced by a close — and neither substitutes for the other.

Everything here is Windows-only and imports its Win32 dependencies inside the
function that needs them, so importing this module on another platform is
harmless and the failure of a missing API names the call that wanted it.
"""

from __future__ import annotations

import contextlib
import os
import sys
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


# ``DuplicateHandle`` takes a BOOL and a flag word in its last two slots. The
# ``_winapi`` binding is positional-only, so these names keep each literal out
# of the argument list and name the policy it encodes.
_DO_NOT_INHERIT_HANDLE = False
_DUPLICATE_SAME_ACCESS = 2


class _HandleApi(typ.NamedTuple):
    """The three ``_winapi`` entry points a handle transfer needs."""

    current_process: cabc.Callable[[], int]
    duplicate_handle: cabc.Callable[..., int]
    close_handle: cabc.Callable[[int], None]


def _winapi_handle_api() -> _HandleApi:
    """Load the current-process and handle entry points from ``_winapi``.

    ``_winapi`` is a private CPython module, and typeshed declares its whole
    body under a Windows-only guard, so on a non-Windows host every one of
    these names is absent from the analysed module. Resolving them through
    ``getattr`` keeps the call sites type-checkable there, and mirrors how
    ``msvcrt``'s entry points are reached below.

    Returns
    -------
    _HandleApi
        The current-process, duplicate and close entry points, resolved from
        whichever ``_winapi`` build is installed.
    """
    import _winapi

    return _HandleApi(
        current_process=typ.cast(
            "cabc.Callable[[], int]",
            getattr(_winapi, "GetCurrentProcess"),  # ruff: ignore[get-attr-with-constant]  # Windows-only API.
        ),
        duplicate_handle=typ.cast(
            "cabc.Callable[..., int]",
            getattr(_winapi, "DuplicateHandle"),  # ruff: ignore[get-attr-with-constant]  # Windows-only API.
        ),
        close_handle=typ.cast(
            "cabc.Callable[[int], None]",
            getattr(_winapi, "CloseHandle"),  # ruff: ignore[get-attr-with-constant]  # Windows-only API.
        ),
    )


def _duplicate_windows_handle(handle: int) -> int:
    """Duplicate a Win32 handle so Rust can own it independently of the CRT."""
    api = _winapi_handle_api()
    current_process = api.current_process()
    duplicate = api.duplicate_handle(
        current_process,
        handle,
        current_process,
        0,
        _DO_NOT_INHERIT_HANDLE,
        _DUPLICATE_SAME_ACCESS,
    )
    # Rust treats the handle it receives as an owned resource and would close
    # whatever number arrived, so a null result is rejected here rather than
    # smuggled across as a valid handle.
    if duplicate == 0:
        msg = "DuplicateHandle returned a null handle"
        raise OSError(msg)
    return duplicate


def _close_windows_handle(handle: int) -> None:
    """Close a duplicated Win32 handle that was not handed to Rust."""
    _winapi_handle_api().close_handle(handle)


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

    import msvcrt

    get_osfhandle = typ.cast(
        "cabc.Callable[[int], int]",
        getattr(msvcrt, "get_osfhandle"),  # ruff: ignore[get-attr-with-constant]  # Windows-only API.
    )
    handle = get_osfhandle(fd)
    # ``get_osfhandle`` reports a raw HANDLE, which is signed when its top bit
    # is set. The native ``PlatformFd`` is a ``usize``, but the value crosses
    # PyO3 as an ``i64``, so a sign-extended handle has to be narrowed back to
    # the pointer's own width first. ``sys.maxsize`` is ``2**63 - 1`` on a
    # 64-bit interpreter and ``2**31 - 1`` on a 32-bit one, so its bit length
    # plus the sign bit is exactly the pointer width. A handle whose narrowed
    # value still exceeds ``i64::MAX`` cannot cross the boundary and is
    # rejected by the caller's post-conversion range check.
    pointer_bits = sys.maxsize.bit_length() + 1
    return handle & ((1 << pointer_bits) - 1)
