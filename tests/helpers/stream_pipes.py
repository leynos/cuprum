"""Internal helpers for stream pipe tests."""

from __future__ import annotations

import contextlib
import os
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType


def _safe_close(fd: int) -> None:
    """Close a file descriptor, ignoring errors."""
    with contextlib.suppress(OSError):
        os.close(fd)


def _read_all(fd: int, *, chunk_size: int = 4096) -> bytes:
    """Read all data from a file descriptor until EOF."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, chunk_size)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


@contextlib.contextmanager
def _pipe_pair() -> cabc.Iterator[tuple[int, int, int, int]]:
    """Manage pipe creation and cleanup for stream tests."""
    in_read, in_write = os.pipe()
    out_read, out_write = os.pipe()
    try:
        yield in_read, in_write, out_read, out_write
    finally:
        _safe_close(in_read)
        _safe_close(in_write)
        _safe_close(out_read)
        _safe_close(out_write)


def _feed_pipe(write_fd: int, payload: bytes, errors: list[BaseException]) -> None:
    """Write ``payload`` into ``write_fd``, appending any failure to ``errors``.

    Shared by the writer closures in :func:`feed_source_pipe` and
    :func:`pump_payload_through_pipes`. A blocking pipe write never reports zero
    progress, so a non-positive result is recorded as a fault rather than spun
    on (avoiding a bare ``assert``). Descriptor cleanup stays with each caller.
    """
    try:
        view = memoryview(payload)
        while view:
            written = os.write(write_fd, view)
            if written <= 0:
                errors.append(RuntimeError("os.write made no progress"))
                break
            view = view[written:]
    except OSError as exc:
        errors.append(exc)


_JOIN_TIMEOUT_SECONDS = 10.0


def _join_or_raise(thread: threading.Thread, label: str) -> None:
    """Join ``thread`` within a bounded timeout, raising if it stays alive.

    A worker thread that never terminates would otherwise hang the join
    indefinitely; bounding it converts a deadlock into a clear failure that
    names the offending ``label`` worker.
    """
    thread.join(_JOIN_TIMEOUT_SECONDS)
    if thread.is_alive():
        msg = f"{label} thread did not terminate within {_JOIN_TIMEOUT_SECONDS}s"
        raise RuntimeError(msg)


@contextlib.contextmanager
def feed_source_pipe(
    write_fd: int,
    payload: bytes,
    *,
    cancel_fd: int,
) -> cabc.Iterator[None]:
    """Feed ``payload`` into ``write_fd`` from a background thread.

    The writer runs concurrently with the ``with`` body so a synchronous
    consumer can drain the pipe without the write blocking on a full buffer —
    important when the host pipe capacity is smaller than the payload. The
    writer always closes ``write_fd`` once the payload is drained, and is always
    joined on exit. If the body raises, ``cancel_fd`` (the consumer's read end)
    is closed first so a writer stalled on a full pipe cannot deadlock the join.
    Any error raised inside the writer thread is re-raised after cleanup.

    Parameters
    ----------
    write_fd : int
        Writable pipe file descriptor the payload is fed into; the helper
        closes it once the payload is drained.
    payload : bytes
        The bytes written into ``write_fd``.
    cancel_fd : int
        The consumer's read-end descriptor. Closed if the ``with`` body raises,
        breaking a writer stalled on a full pipe so the join cannot deadlock.

    Yields
    ------
    None
        Control passes to the ``with`` body while the writer thread runs.
    """
    errors: list[BaseException] = []

    def writer() -> None:
        """Feed the source pipe, capturing any failure for the main thread."""
        try:
            _feed_pipe(write_fd, payload, errors)
        finally:
            _safe_close(write_fd)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        yield
    except BaseException:
        _safe_close(cancel_fd)
        raise
    finally:
        _join_or_raise(thread, "feed_source_pipe writer")
    if errors:
        raise errors[0]


def pump_payload_through_pipes(
    pump: cabc.Callable[[int, int], int],
    payload: bytes,
) -> tuple[bytes, int]:
    """Feed ``payload`` through ``pump`` using concurrent writer/reader threads.

    A writer thread fills the source pipe while ``pump`` transfers bytes to the
    destination pipe and a reader thread drains it. ``pump`` receives the source
    read end and destination write end and returns the number of bytes
    transferred.

    The worker lifecycle is exception-safe: the destination write end and the
    source read end are always closed and both threads are always joined, even
    when ``pump`` raises. Closing those ends first unblocks a reader waiting on
    EOF and a writer blocked on a full pipe, so a failing pump can never leave a
    dangling thread or hang the test. Exceptions raised inside the worker
    threads are captured and re-raised after cleanup rather than being swallowed
    by the threading machinery; a ``pump`` failure takes precedence and
    propagates directly.

    Parameters
    ----------
    pump : collections.abc.Callable[[int, int], int]
        Callback invoked with the source read end and destination write end; it
        transfers bytes and returns the number transferred.
    payload : bytes
        The bytes fed into the source pipe for ``pump`` to transfer.

    Returns
    -------
    tuple[bytes, int]
        The drained destination bytes and the count reported by ``pump``.
    """
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        output_chunks: list[bytes] = []
        errors: list[BaseException] = []

        def writer() -> None:
            """Feed the source pipe, capturing any failure for the main thread."""
            try:
                _feed_pipe(in_write, payload, errors)
            finally:
                _safe_close(in_write)

        def reader() -> None:
            """Drain the destination pipe, capturing any failure."""
            try:
                output_chunks.append(_read_all(out_read))
            except OSError as exc:
                errors.append(exc)

        write_thread = threading.Thread(target=writer)
        read_thread = threading.Thread(target=reader)
        write_thread.start()
        read_thread.start()

        try:
            transferred = pump(in_read, out_write)
        finally:
            # Close the destination write end (EOF for the reader) and the
            # source read end (breaks a writer blocked on a full pipe) before
            # joining, so the joins cannot deadlock when the pump fails.
            _safe_close(out_write)
            _safe_close(in_read)
            _join_or_raise(write_thread, "pump writer")
            _join_or_raise(read_thread, "pump reader")

    if errors:
        raise errors[0]
    return output_chunks[0], transferred


def _pump_rust_stream_payload(
    streams: ModuleType,
    payload: bytes,
    *,
    buffer_size: int | None = None,
) -> tuple[bytes, int]:
    """Pump ``payload`` through the Rust stream pump with concurrent pipes.

    Parameters
    ----------
    streams : types.ModuleType
        The Rust streams module exposing ``rust_pump_stream``.
    payload : bytes
        The bytes to pump through the native stream pump.
    buffer_size : int | None, optional
        Optional pump buffer size forwarded to ``rust_pump_stream`` when set.

    Returns
    -------
    tuple[bytes, int]
        The drained destination bytes and the transferred byte count.
    """
    kwargs: dict[str, int] = {}
    if buffer_size is not None:
        kwargs["buffer_size"] = buffer_size

    def pump(in_read: int, out_write: int) -> int:
        """Run the Rust stream pump across the supplied pipe ends."""
        return streams.rust_pump_stream(in_read, out_write, **kwargs)

    return pump_payload_through_pipes(pump, payload)
