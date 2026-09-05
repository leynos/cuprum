r"""Line-bounded echo helpers shared by the stream-drain loop.

Echoing a child stream line-by-line must respect a byte bound so one oversized
child line cannot overflow a CI job log (GitHub Actions stops accepting output
at a 64 KiB line). Capture stays byte-for-byte complete; only the mirrored copy
is bounded. These helpers are pure so the drain loop and the tests agree on the
truncation contract without subprocess I/O.

A *segment* is the raw bytes of one line without its ``\n`` terminator, so a
``\r\n`` ending leaves the carriage return in the segment and it counts
towards the bound.
"""

from __future__ import annotations

import dataclasses as dc

_TRUNCATION_MARKER_TEMPLATE = "… [truncated {dropped} bytes]"


def truncation_marker(dropped: int, *, encoding: str) -> bytes:
    """Encode the truncation marker for *dropped* bytes.

    Parameters
    ----------
    dropped : int
        Number of bytes dropped from the mirrored line.
    encoding : str
        Encoding used for the echo sink.

    Returns
    -------
    bytes
        The encoded ``… [truncated N bytes]`` marker.
    """
    return _TRUNCATION_MARKER_TEMPLATE.format(dropped=dropped).encode(encoding)


@dc.dataclass(slots=True)
class _EchoLineLimiter:
    r"""Track per-line byte accounting for one echoing stream.

    The limiter consumes raw child bytes (split on ``\n`` terminators) and
    returns the prefix of each line that may still be mirrored. Capture is
    unaffected: callers feed the limiter a copy of the chunk they already
    buffered, or split from it.
    """

    max_line_bytes: int
    emitted_line_bytes: int = 0
    dropped_line_bytes: int = 0

    @classmethod
    def from_config(
        cls,
        *,
        echo_output: bool,
        echo_max_line_bytes: int | None,
    ) -> _EchoLineLimiter | None:
        """Build the limiter for a stream, or ``None`` when unbounded.

        Parameters
        ----------
        echo_output : bool
            Whether the stream is echoed at all.
        echo_max_line_bytes : int | None
            Configured per-line byte bound; ``None`` means unbounded.

        Returns
        -------
        _EchoLineLimiter | None
            A limiter when bounded echoing is active, otherwise ``None``.
        """
        if not echo_output or echo_max_line_bytes is None:
            return None
        return cls(max_line_bytes=echo_max_line_bytes)

    def bound_line(self, segment: bytes) -> bytes:
        r"""Return the prefix of *segment* that may still be mirrored.

        The segment is a raw line body without its ``\n`` terminator. Bytes
        beyond the bound are counted as dropped so the terminator handler can
        emit the truncation marker before the line ending.

        Parameters
        ----------
        segment : bytes
            Raw bytes of the next (possibly partial) line segment.

        Returns
        -------
        bytes
            The bytes of *segment* still allowed through the bound.
        """
        room = self.max_line_bytes - self.emitted_line_bytes
        if room <= 0:
            self.dropped_line_bytes += len(segment)
            return b""
        if len(segment) <= room:
            self.emitted_line_bytes += len(segment)
            return segment
        self.emitted_line_bytes = self.max_line_bytes
        self.dropped_line_bytes += len(segment) - room
        return segment[:room]

    def finish_line(self, *, encoding: str) -> bytes | None:
        """Return the marker to write before the line ending, if any.

        Parameters
        ----------
        encoding : str
            Encoding used for the echo sink; the marker is encoded with it.

        Returns
        -------
        bytes | None
            The encoded truncation marker when bytes were dropped from the
            line, otherwise ``None``. Both counters reset so the next line
            starts from an empty bound.
        """
        if self.dropped_line_bytes == 0:
            self.emitted_line_bytes = 0
            return None
        marker = truncation_marker(self.dropped_line_bytes, encoding=encoding)
        self.emitted_line_bytes = 0
        self.dropped_line_bytes = 0
        return marker
