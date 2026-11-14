"""Optional Rust-backed stream operations for high-throughput workloads.

This module provides a thin wrapper around the optional Rust extension. Import
or use it only when the Rust backend is installed.

Integration status: ``rust_pump_stream`` is wired into production through
``_pump_stream_dispatch`` (``cuprum/_pipeline_streams.py``).
``rust_consume_stream`` is **implemented but not yet integrated** — no
production code routes a consume through it; every consume currently uses the
pure-Python ``_consume_stream``. Consume-side dispatch is deliberately
deferred, evidence-gated work tracked as Phase 2 of ADR-002
(``docs/adrs/adr-0002-additional-rust-components.md``).

Example
-------
bytes_written = rust_pump_stream(reader_fd, writer_fd, buffer_size=65536)
output = rust_consume_stream(reader_fd, buffer_size=65536)
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import logging
import operator
import os
import typing as typ

from cuprum import _pipeline_stream_cleanup_observation as _pump_obs
from cuprum._streams_windows import (
    _convert_fd_for_platform,
    _duplicate_binary_reader,
    _transfer_writer_fd_for_platform,
)
from cuprum.pump_events import RustPumpHandoffOutcome
from cuprum.pump_observation import (
    _emit_native_pump_io_failure,
    _emit_rust_pump_handoff_outcome,
)
from cuprum.pump_span_events import NATIVE_PUMP_BUFFER_SIZE

if typ.TYPE_CHECKING:
    import collections.abc as cabc


_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1
_MAX_BUFFER_SIZE = 1 << 30

_LOGGER = logging.getLogger(__name__)


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


class _PreparedRustPumpCall(typ.NamedTuple):
    """Prepared inputs that leave writer ownership independent."""

    native_pump: cabc.Callable[..., int]
    reader: int
    buffer_size: int


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


def _close_writer_after_pre_native_failure(writer_fd: int) -> None:
    """Close a writer that never reached the native ownership boundary."""
    with contextlib.suppress(OSError):
        os.close(writer_fd)


def _validate_buffer_size_before_writer_transfer(buffer_size: int) -> int:
    """Raise native-compatible buffer-size errors before transferring a writer."""
    size = operator.index(buffer_size)
    if not _I64_MIN <= size <= _I64_MAX:
        msg = "Python int too large to convert to C long"
        raise OverflowError(msg)
    if size <= 0:
        msg = "buffer_size must be greater than zero"
        raise ValueError(msg)
    if size > _MAX_BUFFER_SIZE:
        msg = "buffer_size exceeds the maximum permitted size"
        raise ValueError(msg)
    return size


def _prepare_native_reader(reader_fd: int) -> int:
    """Validate ABI representation while Python still owns the writer."""
    # A negative value is rejected before the platform conversion: on Windows
    # the conversion calls ``msvcrt.get_osfhandle``, which raises ``OSError``
    # for a negative CRT descriptor rather than the documented ``ValueError``.
    raw_reader = operator.index(reader_fd)
    if raw_reader < 0:
        resource = "file handle" if os.name == "nt" else "file descriptor"
        msg = f"{resource} must be non-negative"
        raise ValueError(msg)
    reader = operator.index(_convert_fd_for_platform(raw_reader))
    if not _I64_MIN <= reader <= _I64_MAX:
        msg = "Python int too large to convert to C long"
        raise OverflowError(msg)
    if os.name != "nt" and not -(1 << 31) <= reader < (1 << 31):
        msg = "file descriptor out of range"
        raise ValueError(msg)
    return reader


def _prepare_rust_pump_call(
    *,
    reader_fd: int,
    writer_fd: int,
    buffer_size: int,
) -> _PreparedRustPumpCall:
    """Prepare native inputs while Python still owns the writer."""
    try:
        native_pump = _load_native().rust_pump_stream
    except BaseException:
        _close_writer_after_pre_native_failure(writer_fd)
        _emit_rust_pump_handoff_outcome(RustPumpHandoffOutcome.NATIVE_LOAD_FAILED)
        raise
    try:
        validated_buffer_size = _validate_buffer_size_before_writer_transfer(
            buffer_size
        )
    except BaseException:
        _close_writer_after_pre_native_failure(writer_fd)
        _emit_rust_pump_handoff_outcome(RustPumpHandoffOutcome.BUFFER_VALIDATION_FAILED)
        raise
    try:
        reader = _prepare_native_reader(reader_fd)
    except BaseException as error:
        _pump_obs._log_native_pump_handoff_failed(
            _LOGGER,
            "reader_preparation",
            error,
        )
        _close_writer_after_pre_native_failure(writer_fd)
        _emit_rust_pump_handoff_outcome(
            RustPumpHandoffOutcome.READER_PREPARATION_FAILED
        )
        raise
    return _PreparedRustPumpCall(
        native_pump=native_pump,
        reader=reader,
        buffer_size=validated_buffer_size,
    )


def rust_pump_stream(
    reader_fd: int,
    writer_fd: int,
    *,
    buffer_size: int = NATIVE_PUMP_BUFFER_SIZE,
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

    Raises
    ------
    OSError
        If the Rust pump encounters an I/O error, or if Windows rejects a raw
        handle because synchronous-I/O suitability has not been established.
    ValueError
        If the wrapper rejects an argument before any native hand-off:
        ``buffer_size`` that is not positive or exceeds 1 GiB, or a negative
        ``reader_fd``.

    Notes
    -----
    The wrapper validates ``buffer_size`` and ``reader_fd`` before transferring
    a Windows writer resource, preserving the native entry point's errors
    without leaking a duplicated handle. Other failures propagate unchanged
    from the Rust extension: ``ImportError`` if the native module cannot be
    imported and ``OSError`` for native I/O errors. Windows raw-handle
    rejection has its own bounded hand-off observation outcome.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError propagates from _prepare_rust_pump_call.
    prepared = _prepare_rust_pump_call(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
        buffer_size=buffer_size,
    )
    try:
        writer = _transfer_writer_fd_for_platform(writer_fd)
    except BaseException as error:
        _pump_obs._log_native_pump_handoff_failed(
            _LOGGER,
            "platform_writer_transfer",
            error,
        )
        _close_writer_after_pre_native_failure(writer_fd)
        _emit_rust_pump_handoff_outcome(
            RustPumpHandoffOutcome.PLATFORM_WRITER_TRANSFER_FAILED
        )
        raise
    try:
        return int(
            prepared.native_pump(
                prepared.reader,
                writer,
                buffer_size=prepared.buffer_size,
            )
        )
    except OSError:
        _emit_native_pump_io_failure(_LOGGER, is_windows=os.name == "nt")
        raise


def _validate_windows_consume_reader(reader_fd: int) -> int:
    """Reject a CRT descriptor the Windows consume boundary cannot accept.

    The checks mirror the native entry point's, but run before ``os.dup``:
    duplicating first would surface ``os.dup``'s ``OSError`` for a descriptor
    the caller was promised as ``ValueError``, and the duplicate would carry an
    invalid value into a CRT call that fast-fails the process rather than
    raising.

    Parameters
    ----------
    reader_fd : int
        Candidate CRT descriptor supplied by the caller.

    Returns
    -------
    int
        The same descriptor as an ``int``, once it is known to be usable.

    Raises
    ------
    OverflowError
        If the value does not fit PyO3's ``i64`` extraction range.
    ValueError
        If the descriptor is negative or exceeds the CRT's signed 32-bit range.
    """
    reader = operator.index(reader_fd)
    if not _I64_MIN <= reader <= _I64_MAX:
        msg = "Python int too large to convert to C long"
        raise OverflowError(msg)
    if reader < 0:
        msg = "file descriptor must be non-negative"
        raise ValueError(msg)
    if reader > (1 << 31) - 1:
        msg = "file descriptor out of range"
        raise ValueError(msg)
    return reader


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

    On Windows, it duplicates the CRT reader descriptor, sets that duplicate
    to binary mode, and closes it after the native call; the caller keeps
    ownership and its original translation mode. The wrapper rejects a
    negative or out-of-range Windows descriptor and an invalid
    ``buffer_size`` itself, before ``os.dup`` runs. Duplicating first would
    raise ``os.dup``'s ``OSError`` where the native boundary documents
    ``ValueError`` for both arguments, and would also put the native buffer
    check after a descriptor conversion, reversing the order the entry point
    guarantees on every platform.

    Failures propagate unchanged from the Rust extension rather than being
    raised here: ``ImportError`` if the native module cannot be imported,
    ``ValueError`` if ``buffer_size`` is not a positive integer or exceeds the
    1 GiB maximum, and ``OSError`` if descriptor setup or reading fails.
    ``OverflowError`` reports an integer outside PyO3's ``i64`` range.
    """
    native = _load_native()
    if os.name == "nt":
        # Buffer validation precedes descriptor conversion in the native entry
        # point on every platform, so the wrapper keeps that order: it is part
        # of the documented contract, not an implementation detail. Duplicating
        # before either check would run `os.dup` — and its `OSError` — where
        # the caller was promised `ValueError`, and would carry a descriptor
        # the CRT will reject into a call that fast-fails the process instead
        # of returning an error.
        validated_buffer_size = _validate_buffer_size_before_writer_transfer(
            buffer_size
        )
        reader = _validate_windows_consume_reader(reader_fd)
        with _duplicate_binary_reader(reader) as duplicate_fd:
            return native.rust_consume_stream(
                duplicate_fd,
                buffer_size=validated_buffer_size,
            )

    return native.rust_consume_stream(reader_fd, buffer_size=buffer_size)


__all__ = ["rust_consume_stream", "rust_pump_stream"]
