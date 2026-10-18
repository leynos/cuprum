"""PTY-backed profiling sinks with acknowledged output draining."""

from __future__ import annotations

import contextlib
import os
import select
import threading
import typing as typ

from benchmarks import _pty_setup
from benchmarks._pty_setup import _close_unstarted_resources
from benchmarks.errors import PtyBlackholeDrainError, PtyBlackholeStateError

if typ.TYPE_CHECKING:
    import types

    from benchmarks._pty_writer import _CountingPtyWriter


class PtyBlackhole(contextlib.AbstractContextManager[typ.IO[str]]):
    """TTY-like sink with a background drainer for throughput profiling.

    Synchronization contract
    ------------------------
    The master file-descriptor and the read end of a wake pipe are owned
    exclusively by the ``_drain`` daemon thread after ``__enter__`` returns.
    The slave ``IO[str]`` stream returned by ``__enter__`` must only be used
    from the thread that called ``__enter__``. The sink counts bytes accepted
    by the slave after UTF-8-style encoding and accounts for the configured
    newline translation. On exit, it wakes the drainer with that exact byte
    target and closes the slave only after the drainer acknowledges it. The
    drainer sets a completion event only after reaching the target or recording
    a failure. ``drained_bytes`` is available only after successful reader
    acknowledgement and thread termination. A shutdown timeout raises
    ``PtyBlackholeDrainError`` and leaves the PTY open for late acknowledgement
    and deferred cleanup.
    """

    def __init__(self, *, encoding: str, errors: str) -> None:
        """Record the slave-stream encoding for later ``__enter__`` calls."""
        self._encoding = encoding
        self._errors = errors
        self._master_fd: int | None = None
        self._slave: typ.IO[str] | None = None
        self._thread: threading.Thread | None = None
        self._drained_bytes = 0
        self._expected_output_bytes = 0
        self._drain_target: int | None = None
        self._drainer_error: Exception | None = None
        self._counter_lock = threading.Lock()
        self._wake_write_fd: int | None = None
        self._drainer_wake_read_fd: int | None = None
        self._raw_writer: _CountingPtyWriter | None = None
        self._drainer_finished = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._cleanup_deferred = False

    @property
    def drained_bytes(self) -> int | None:
        """Report the number of bytes consumed by the drainer thread.

        Returns
        -------
            int | None
            The total byte count acknowledged by the PTY reader, or ``None``
            while shutdown is pending or if the reader did not reach its target.
        """
        if not self._drainer_has_terminated() or self._drainer_error is not None:
            return None
        if self._drain_target is None or self._drained_bytes != self._drain_target:
            return None
        return self._drained_bytes

    def __enter__(self) -> typ.IO[str]:
        """Open the pseudo-terminal and start draining the master side.

        Returns
        -------
        IO[str]
            The writable slave stream connected to the PTY.

        Raises
        ------
        PtyBlackholeStateError
            If a previous drainer has not yet terminated.
        """
        self._publish_finished_drainer()
        if self._thread is not None:
            msg = "cannot reuse PtyBlackhole while its drainer is still running"
            raise PtyBlackholeStateError(msg)
        self._drained_bytes = 0
        self._expected_output_bytes = 0
        self._drain_target = None
        self._drainer_error = None
        self._counter_lock = threading.Lock()
        self._drainer_finished.clear()
        self._cleanup_deferred = False
        resources = _pty_setup._open_pty_blackhole_resources(
            self._encoding,
            self._errors,
            self._record_expected_bytes,
        )
        self._master_fd = resources.master_fd
        self._slave = resources.slave
        self._raw_writer = resources.raw_writer
        self._wake_write_fd = resources.wake_write_fd
        self._drainer_wake_read_fd = resources.wake_read_fd
        drainer_ready = threading.Event()
        try:
            self._thread = threading.Thread(
                target=self._drain,
                args=(resources.master_fd, resources.wake_read_fd, drainer_ready),
                daemon=True,
            )
            self._thread.start()
        except BaseException:
            _close_unstarted_resources(resources)
            self._master_fd = None
            self._slave = None
            self._thread = None
            self._raw_writer = None
            self._wake_write_fd = None
            self._drainer_wake_read_fd = None
            self._drainer_finished.clear()
            raise
        drainer_ready.wait()
        # The worker owns both descriptors once it has entered _drain.
        self._master_fd = None
        self._drainer_wake_read_fd = None
        return resources.slave

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        """Request exact-byte acknowledgement and close the sink safely.

        Notes
        -----
        A reader failure or a timeout waiting for acknowledgement is reported
        as ``PtyBlackholeDrainError`` when no body exception is active.
        """
        flush_error = self._flush_slave()
        self._publish_drain_target()
        self._wake_drainer()
        drainer_stopped = False
        try:
            drainer_stopped = self._wait_for_drainer()
        finally:
            self._close_or_defer_resources(drainer_stopped=drainer_stopped)
        if exc_type is None:
            self._raise_if_drain_failed(
                flush_error,
                drainer_stopped=drainer_stopped,
            )

    def _flush_slave(self) -> Exception | None:
        """Flush buffered output and retain any failure for teardown."""
        if self._slave is None:
            return None
        try:
            self._slave.flush()
        except (OSError, ValueError) as error:
            return error
        return None

    def _publish_drain_target(self) -> None:
        """Publish the exact output target after the stream has been flushed."""
        if self._raw_writer is None:
            return
        with self._counter_lock:
            self._drain_target = self._expected_output_bytes

    def _wait_for_drainer(self) -> bool:
        """Wait briefly for the reader and report whether it stopped."""
        if self._thread is None:
            return True
        self._thread.join(timeout=5.0)
        drainer_stopped = not self._thread.is_alive()
        if drainer_stopped:
            self._publish_finished_drainer()
        return drainer_stopped

    def _close_resources(self) -> None:
        """Close owned descriptors even when closing the text stream fails."""
        self._close_wake_writer()
        raw_writer = self._raw_writer
        try:
            if self._slave is not None:
                self._slave.close()
        finally:
            if raw_writer is not None:
                with contextlib.suppress(OSError):
                    raw_writer.close()
            self._slave = None
            self._raw_writer = None
            master_fd = self._master_fd
            self._master_fd = None
            if master_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(master_fd)

    def _close_or_defer_resources(self, *, drainer_stopped: bool) -> None:
        """Keep the slave open if the reader still needs to finish its drain."""
        if drainer_stopped:
            self._close_resources()
            return
        with self._lifecycle_lock:
            should_close = self._drainer_finished.is_set()
            if not should_close:
                self._cleanup_deferred = True
        if should_close:
            self._close_resources()

    def _raise_if_drain_failed(
        self,
        flush_error: Exception | None,
        *,
        drainer_stopped: bool,
    ) -> None:
        """Raise a flush or acknowledgement failure when no body error exists."""
        if flush_error is not None:
            raise flush_error
        if not drainer_stopped:
            msg = "timed out waiting for PTY drainer to acknowledge accepted output"
            raise PtyBlackholeDrainError(msg)
        self._raise_if_drain_incomplete()

    def _raise_if_drain_incomplete(self) -> None:
        """Raise when a stopped drainer failed to acknowledge its target."""
        if self._drainer_error is not None:
            msg = "PTY drainer failed before acknowledging accepted output"
            raise PtyBlackholeDrainError(msg) from self._drainer_error
        target = self._drain_target
        if target is not None and self._drained_bytes != target:
            msg = (
                f"PTY drainer acknowledged {self._drained_bytes} of "
                f"{target} output bytes"
            )
            raise PtyBlackholeDrainError(msg)

    def _record_expected_bytes(
        self,
        data: bytes,
        *,
        translates_newlines: bool,
    ) -> None:
        """Track bytes the PTY will expose after configured output processing."""
        translated = data.count(b"\n") if translates_newlines else 0
        with self._counter_lock:
            self._expected_output_bytes += len(data) + translated

    def _wake_drainer(self) -> None:
        """Wake the master reader after publishing its drain target."""
        wake_fd = self._wake_write_fd
        if wake_fd is None:
            return
        # A failed reader closes its pipe end; the stored cause is reported
        # after the bounded join below.
        with contextlib.suppress(OSError):
            os.write(wake_fd, b"\x00")

    def _close_wake_writer(self) -> None:
        """Close the wake pipe only after the reader has finished waiting."""
        wake_fd = self._wake_write_fd
        self._wake_write_fd = None
        if wake_fd is not None:
            with contextlib.suppress(OSError):
                os.close(wake_fd)

    def _publish_finished_drainer(self) -> None:
        """Join a finished drainer and publish its byte count."""
        if self._thread is None or self._thread.is_alive():
            return
        self._thread.join()
        self._thread = None

    def _drainer_has_terminated(self) -> bool:
        """Return whether the drainer has both published and terminated."""
        if not self._drainer_finished.is_set():
            return False
        if self._thread is None:
            return True
        return not self._thread.is_alive()

    def _drain(
        self,
        master_fd: int,
        wake_read_fd: int,
        ready: threading.Event,
    ) -> None:
        """Read PTY output until the writer's exact target is acknowledged.

        Notes
        -----
        Before exit publishes a target, reads continue to keep the PTY buffer
        from filling during benchmark writes. The wake pipe lets an empty sink
        and a drainer blocked in ``select`` observe the final target.
        """
        try:
            ready.set()
            self._drain_until_target(master_fd, wake_read_fd)
        except (OSError, ValueError, PtyBlackholeDrainError) as error:
            self._drainer_error = error
        finally:
            with contextlib.suppress(OSError):
                os.close(master_fd)
            with contextlib.suppress(OSError):
                os.close(wake_read_fd)
            with self._lifecycle_lock:
                self._drainer_finished.set()
                cleanup_deferred = self._cleanup_deferred
                self._cleanup_deferred = False
            if cleanup_deferred:
                try:
                    self._close_resources()
                except (OSError, ValueError) as error:
                    self._drainer_error = error

    def _drain_until_target(self, master_fd: int, wake_read_fd: int) -> None:
        """Read until the writer's published target has been acknowledged."""
        while True:
            target = self._get_drain_target()
            if self._target_reached(target, self._drained_bytes):
                return
            self._drain_read_cycle(master_fd, wake_read_fd)

    def _drain_read_cycle(self, master_fd: int, wake_read_fd: int) -> None:
        """Wait for output or a target update, then consume one readable chunk."""
        readable, _, _ = select.select((master_fd, wake_read_fd), (), ())
        target = self._target_after_wake(
            wake_read_fd,
            wake_is_readable=wake_read_fd in readable,
        )
        if self._target_reached(target, self._drained_bytes):
            return
        if master_fd not in readable:
            return
        chunk = self._read_chunk(master_fd, target, self._drained_bytes)
        if chunk is not None:
            self._drained_bytes += len(chunk)

    def _target_after_wake(
        self,
        wake_read_fd: int,
        *,
        wake_is_readable: bool,
    ) -> int | None:
        """Read the published target, consuming the control byte when present."""
        if not wake_is_readable:
            return self._get_drain_target()
        wake = os.read(wake_read_fd, 1)
        if not wake:
            msg = "PTY drain wake pipe closed without a target"
            raise PtyBlackholeDrainError(msg)
        target = self._get_drain_target()
        if target is None:
            msg = "PTY drain target was not published before wake"
            raise PtyBlackholeDrainError(msg)
        return target

    @staticmethod
    def _target_reached(target: int | None, drained_bytes: int) -> bool:
        """Validate the count and report whether the published target is met."""
        if target is None:
            return False
        if drained_bytes > target:
            msg = "PTY produced more output than the published target"
            raise PtyBlackholeDrainError(msg)
        return drained_bytes == target

    @staticmethod
    def _read_chunk(
        master_fd: int,
        target: int | None,
        drained_bytes: int,
    ) -> bytes | None:
        """Read available target bytes, retrying if PTY readiness was transient."""
        read_size = 65536
        if target is not None:
            read_size = min(read_size, target - drained_bytes)
        try:
            chunk = os.read(master_fd, read_size)
        except BlockingIOError:
            return None
        if not chunk:
            msg = "PTY master closed before the drain target was reached"
            raise PtyBlackholeDrainError(msg)
        return chunk

    def _get_drain_target(self) -> int | None:
        """Read the published target while holding the accounting lock."""
        with self._counter_lock:
            return self._drain_target
