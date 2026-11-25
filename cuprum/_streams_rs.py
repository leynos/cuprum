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
from cuprum._constants import DEFAULT_NATIVE_BUFFER_SIZE
from cuprum._stream_argument_policy import (
    _I64_MAX,
    _I64_MIN,
    _validate_buffer_size,
    _validate_raw_reader,
)
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


def _prepare_native_reader(reader_fd: int) -> int:
    """Validate a raw reader, then convert it for the native platform.

    Validation happens while Python still owns the writer. The converted
    Windows handle may be pointer-sized rather than a 32-bit descriptor, so the
    raw ``i32`` check belongs to :func:`_validate_raw_reader` and only the
    ``i64`` representation is rechecked here.

    Parameters
    ----------
    reader_fd : int
        Raw reader descriptor to validate and convert.

    Returns
    -------
    int
        The platform value the native entry point receives.

    Raises
    ------
    OverflowError
        If the converted value does not fit PyO3's ``i64`` range.
    """
    raw_reader = _validate_raw_reader(reader_fd)
    reader = operator.index(_convert_fd_for_platform(raw_reader))
    if not _I64_MIN <= reader <= _I64_MAX:
        msg = "Python int too large to convert to C long"
        raise OverflowError(msg)
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
        validated_buffer_size = _validate_buffer_size(buffer_size)
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


def _pump_through_transferred_writer(
    prepared: _PreparedRustPumpCall,
    writer_fd: int,
) -> int:
    """Transfer the writer, pump through it, and observe either failure."""
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
        Descriptor to read from. Any object implementing ``__index__`` is
        accepted; the normalized value must be non-negative and within the
        signed 32-bit descriptor range.
    writer_fd : int
        Descriptor to write to. On Windows it is converted to an owned Win32
        handle and the CRT descriptor is closed here; elsewhere Rust adopts
        the descriptor itself.
    buffer_size : int, optional
        Bytes per read/write cycle. Must be an index-like value greater than
        zero and no larger than 1 GiB (``1 << 30``). Defaults to
        ``NATIVE_PUMP_BUFFER_SIZE`` (64 KiB).

    Returns
    -------
    int
        The number of bytes successfully written.

    Raises
    ------
    ImportError
        If the native module cannot be imported.
    OverflowError
        If ``buffer_size`` or ``reader_fd`` does not fit PyO3's ``i64`` range.
    ValueError
        If ``buffer_size`` is not positive or exceeds 1 GiB, or if
        ``reader_fd`` is negative or above the signed 32-bit descriptor
        range.
    OSError
        If the Rust pump encounters an I/O error, if platform writer transfer
        fails, or if Windows rejects a raw handle because synchronous-I/O
        suitability has not been established.

    Notes
    -----
    Validation order is fixed and shared with the consume wrapper: load the
    native module, validate ``buffer_size``, validate ``reader_fd``, convert
    the reader for the platform, then transfer ``writer_fd``. ``buffer_size``
    is validated completely, stream-domain check included, before
    ``reader_fd`` is inspected, so a competing reader failure cannot pre-empt
    a buffer error. Only once the buffer is accepted can extracting
    ``reader_fd`` raise ``TypeError`` or ``OverflowError``, and those precede
    its own stream-domain check.

    Every pre-native failure closes the writer Python still owns and emits the
    matching hand-off outcome; a writer that reaches Rust is adopted and closed
    there. The gap between validation and adoption is therefore observable
    only as the wrapper's cleanup: no argument is rechecked after the transfer.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError propagates from _prepare_rust_pump_call.
    prepared = _prepare_rust_pump_call(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
        buffer_size=buffer_size,
    )
    return _pump_through_transferred_writer(prepared, writer_fd)


def rust_consume_stream(
    reader_fd: int,
    *,
    buffer_size: int = DEFAULT_NATIVE_BUFFER_SIZE,
) -> str:
    """Consume bytes from a file descriptor using the Rust extension.

    This helper always decodes UTF-8 and replaces invalid sequences with the
    Unicode replacement character.

    Parameters
    ----------
    reader_fd : int
        Descriptor to read from. Any object implementing ``__index__`` is
        accepted; the normalized value must be non-negative and within the
        signed 32-bit descriptor range.
    buffer_size : int, optional
        Bytes per read cycle. Must be an index-like value greater than zero
        and no larger than 1 GiB (``1 << 30``). Defaults to
        ``DEFAULT_NATIVE_BUFFER_SIZE`` (64 KiB).

    Returns
    -------
    str
        Decoded output from the stream.

    Raises
    ------
    ImportError
        If the native module cannot be imported.
    OverflowError
        If ``buffer_size`` or ``reader_fd`` does not fit PyO3's ``i64`` range.
    ValueError
        If ``buffer_size`` is not positive or exceeds 1 GiB, or if
        ``reader_fd`` is negative or above the signed 32-bit descriptor
        range.
    OSError
        If descriptor setup or reading fails.

    Notes
    -----
    Implemented but not yet integrated. No production code path routes a
    consume through this function; the pump side has a
    ``_pump_stream_dispatch`` counterpart, but consume dispatch is deferred,
    evidence-gated work (ADR-002, Phase 2). The function is exercised directly
    by tests and remains available for downstream experimentation.

    Validation order matches the pump wrapper: load the native module, check
    ``buffer_size``, check ``reader_fd``, apply the platform step, then call
    native. Both arguments are checked in Python, so neither an invalid buffer
    nor a negative or oversized descriptor reaches ``os.dup`` on Windows, where
    the CRT would otherwise report ``OSError`` for a value documented as
    ``ValueError``. An in-range descriptor that is already closed can still
    reach the native read, so the caller must keep it open. On Unix the native
    entry rechecks the same policy and the wrapper passes the raw descriptor
    through.

    On Windows the wrapper duplicates the CRT reader, sets only that duplicate
    to binary mode, and closes it after the native call. The caller keeps
    ownership and its original translation mode. On Unix the reader is
    borrowed for the synchronous native call and remains open and usable
    afterwards. The native boundary still owns its own duplication on Windows,
    so a direct native caller is equally safe.
    """  # ruff: ignore[docstring-extraneous-exception] - ImportError propagates from _load_native.
    native = _load_native()
    validated_buffer_size = _validate_buffer_size(buffer_size)
    reader = _validate_raw_reader(reader_fd)
    if os.name == "nt":
        # The validated CRT descriptor is duplicated before the native call so
        # the caller's translation mode is untouched while the GIL is released.
        with _duplicate_binary_reader(reader) as duplicate_fd:
            return native.rust_consume_stream(
                duplicate_fd,
                buffer_size=validated_buffer_size,
            )

    return native.rust_consume_stream(reader, buffer_size=validated_buffer_size)


__all__ = ["rust_consume_stream", "rust_pump_stream"]
