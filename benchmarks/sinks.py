"""Sink implementations for tee-path profiling scenarios."""

from __future__ import annotations

import contextlib
import io
import os
import pathlib as pth
import pty
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import types

type SinkKind = typ.Literal["devnull", "text_blackhole", "pty_blackhole"]


class TextBlackhole(io.TextIOBase):
    """Text sink that accepts strings without exposing a byte buffer."""

    @typ.override
    def writable(self) -> bool:
        """Return that this stream accepts writes."""
        return True

    @typ.override
    def write(self, s: str) -> int:
        """Discard text and report the accepted character count."""
        if not isinstance(s, str):
            msg = f"expected str, got {type(s).__name__}"
            raise TypeError(msg)
        return len(s)

    def flush(self) -> None:
        """Flush the blackhole sink."""


class PtyBlackhole(contextlib.AbstractContextManager[typ.IO[str]]):
    """TTY-like sink with a background drainer for throughput profiling.

    Synchronisation contract
    ------------------------
    The master file-descriptor is owned exclusively by the ``_drain`` daemon
    thread after ``__enter__`` returns. The slave ``IO[str]`` stream returned
    by ``__enter__`` must only be used from the thread that called
    ``__enter__``. ``__exit__`` closes the slave first, which causes the
    master-side ``os.read`` in ``_drain`` to raise ``OSError`` and return,
    then waits up to five seconds for the thread to finish. No lock is needed
    because the two threads access disjoint file descriptors.
    """

    def __init__(self, *, encoding: str, errors: str) -> None:
        self._encoding = encoding
        self._errors = errors
        self._master_fd: int | None = None
        self._slave: typ.IO[str] | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> typ.IO[str]:
        """Open the pseudo-terminal and start draining the master side."""
        master_fd, slave_fd = pty.openpty()
        slave: typ.IO[str] | None = None
        try:
            self._master_fd = master_fd
            slave = os.fdopen(
                slave_fd,
                "w",
                buffering=1,
                encoding=self._encoding,
                errors=self._errors,
            )
            self._slave = slave
            self._thread = threading.Thread(target=self._drain, daemon=True)
            self._thread.start()
        except (LookupError, OSError, RuntimeError):
            if slave is None:
                with contextlib.suppress(OSError):
                    os.close(slave_fd)
            else:
                with contextlib.suppress(OSError):
                    slave.close()
            with contextlib.suppress(OSError):
                os.close(master_fd)
            self._master_fd = None
            self._slave = None
            self._thread = None
            raise
        return slave

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> bool | None:
        """Close the sink and wait briefly for the drainer to finish."""
        if self._slave is not None:
            self._slave.close()
            self._slave = None
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        return None

    def _drain(self) -> None:
        """Continuously read and discard bytes from the PTY master."""
        master_fd = self._master_fd
        if master_fd is None:
            return
        while True:
            try:
                chunk = os.read(master_fd, 65536)
            except OSError:
                return
            if not chunk:
                return


@contextlib.contextmanager
def open_sink(
    kind: SinkKind,
    *,
    encoding: str,
    errors: str,
) -> typ.Iterator[typ.IO[str]]:
    """Open a profiling sink by kind.

    Parameters
    ----------
    kind:
        Sink variant: ``"devnull"`` (OS null device), ``"text_blackhole"``
        (character counter), or ``"pty_blackhole"`` (pseudo-terminal drained
        by a daemon thread).
    encoding:
        Text encoding forwarded to the underlying stream or PTY slave.
    errors:
        Error-handling scheme forwarded to the underlying stream.

    Returns
    -------
    Iterator[IO[str]]
        Context-managed writable text stream; resources are released on exit.
    """
    if kind == "devnull":
        with pth.Path(os.devnull).open("w", encoding=encoding, errors=errors) as stream:
            yield stream
        return
    if kind == "text_blackhole":
        yield TextBlackhole()
        return
    if kind == "pty_blackhole":
        with PtyBlackhole(encoding=encoding, errors=errors) as stream:
            yield stream
        return
    typ.assert_never(kind)
