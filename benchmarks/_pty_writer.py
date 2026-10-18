"""Raw writer used to count accepted PTY slave output."""

from __future__ import annotations

import io
import os
import typing as typ

from benchmarks.errors import PtyBlackholeStateError

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _CountingPtyWriter(io.RawIOBase):
    """Write encoded bytes to a PTY and report bytes accepted by its slave."""

    def __init__(
        self,
        fd: int,
        on_bytes_written: cabc.Callable[[bytes], None],
    ) -> None:
        """Record the slave descriptor and output-byte accounting callback."""
        self._fd = fd
        self._on_bytes_written = on_bytes_written

    @typ.override
    def writable(self) -> bool:
        """Return whether this writer can accept bytes."""
        return not self.closed

    @typ.override
    def fileno(self) -> int:
        """Return the owned PTY slave descriptor."""
        if self.closed:
            msg = "I/O operation on closed PTY stream"
            raise PtyBlackholeStateError(msg)
        return self._fd

    @typ.override
    def isatty(self) -> bool:
        """Return whether the owned descriptor is connected to a terminal."""
        return os.isatty(self._fd)

    @typ.override
    def write(self, data: cabc.Buffer, /) -> int:
        """Write all bytes, counting each prefix accepted by the PTY."""
        if self.closed:
            msg = "write to closed PTY stream"
            raise PtyBlackholeStateError(msg)
        view = memoryview(data)
        written_total = 0
        while written_total < len(view):
            written = os.write(self._fd, view[written_total:])
            if written == 0:
                msg = "PTY slave accepted no output bytes"
                raise OSError(msg)
            start = written_total
            written_total += written
            self._on_bytes_written(bytes(view[start:written_total]))
        return written_total

    @typ.override
    def close(self) -> None:
        """Close the owned PTY slave descriptor."""
        if self.closed:
            return
        try:
            os.close(self._fd)
        finally:
            super().close()
