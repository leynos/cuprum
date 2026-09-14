r"""Line-bounded echo helpers shared by the stream-drain loop.

Echoing a child stream line-by-line must respect a byte bound so one oversized
child line cannot overflow a CI job log (GitHub Actions stops accepting output
at a 64 KiB line). Capture stays byte-for-byte complete; only the mirrored copy
is bounded. These helpers are pure so the drain loop and the tests agree on the
truncation contract without subprocess I/O.

A *segment* is the raw bytes of one line without its terminator. The stream
drain identifies ``\r\n`` before a segment reaches this module, so the whole
terminator is accounted for as a line ending.
"""

from __future__ import annotations

import codecs
import dataclasses as dc

_TRUNCATION_MARKER_TEMPLATE = "… [truncated {dropped} bytes]"
_ASCII_TRUNCATION_MARKER_TEMPLATE = "... [truncated {dropped} bytes]"
_STATEFUL_BOUNDED_ECHO_ENCODINGS = frozenset({"hz", "utf_7", "utf_8_sig"})


def truncation_marker(dropped: int, *, encoding: str, errors: str) -> bytes:
    """Encode the truncation marker for *dropped* bytes.

    Parameters
    ----------
    dropped : int
        Number of bytes dropped from the mirrored line.
    encoding : str
        Encoding used for the echo sink.
    errors : str
        Error policy used if the ASCII fallback must be encoded.

    Returns
    -------
    bytes
        The encoded ``… [truncated N bytes]`` marker.
    """
    preferred = _TRUNCATION_MARKER_TEMPLATE.format(dropped=dropped)
    try:
        return preferred.encode(encoding)
    except UnicodeEncodeError:
        fallback = _ASCII_TRUNCATION_MARKER_TEMPLATE.format(dropped=dropped)
        return fallback.encode(encoding, errors)


def _validate_bounded_echo_encoding(encoding: str, errors: str) -> None:
    """Reject codecs that cannot safely represent raw bounded-line segments."""
    codec = codecs.lookup(encoding)
    newline = "\n".encode(codec.name, errors)
    is_stateful = (
        codec.name in _STATEFUL_BOUNDED_ECHO_ENCODINGS
        or codec.name.startswith("iso2022_")
    )
    if is_stateful or newline != b"\n":
        msg = (
            "bounded echo requires an ASCII-compatible stateless encoding; "
            f"got {encoding!r}. Set max_echo_line_bytes=None to echo it unbounded."
        )
        raise ValueError(msg)


@dc.dataclass(frozen=True, slots=True)
class _FinishedEchoLine:
    """Bytes ready for one echo write and the source bytes omitted from it."""

    payload: bytes
    dropped_bytes: int


@dc.dataclass(frozen=True, slots=True)
class _EchoEncoding:
    """Encoding settings used to assemble an echoed line."""

    encoding: str
    errors: str
    is_text_sink: bool


@dc.dataclass(frozen=True, slots=True)
class _LineFinalization:
    """Source accounting and echoed terminator for one completed line."""

    source_bytes: int
    ending: bytes
    omitted_ending_bytes: int


@dc.dataclass(slots=True)
class _EchoLineLimiter:
    r"""Track per-line byte accounting for one echoing stream.

    The limiter consumes raw child bytes (split on ``\n`` terminators) and
    returns the prefix of each line that may still be mirrored. Capture is
    unaffected: callers feed the limiter a copy of the chunk they already
    buffered, or split from it.
    """

    max_line_bytes: int
    _line: bytearray = dc.field(default_factory=bytearray)
    _source_line_bytes: int = 0
    has_pending_carriage_return: bool = False

    @property
    def has_line_bytes(self) -> bool:
        """Whether the current logical line has buffered source bytes."""
        return self._source_line_bytes > 0

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

    def bound_line(self, segment: bytes) -> None:
        """Account for source bytes while retaining only the bounded prefix."""
        self._source_line_bytes += len(segment)
        remaining = self.max_line_bytes - len(self._line)
        if remaining > 0:
            self._line.extend(segment[:remaining])

    def finish_line(
        self,
        *,
        ending: bytes,
        encoding: str,
        errors: str,
        is_text_sink: bool,
    ) -> _FinishedEchoLine:
        """Finish one line without exceeding its mirrored byte budget.

        Returns
        -------
        _FinishedEchoLine
            The bounded echo payload and its omitted source-byte count.
        """
        line = bytes(self._line)
        self._line.clear()
        source_line_bytes = self._source_line_bytes
        self._source_line_bytes = 0
        finalization = _LineFinalization(
            source_bytes=source_line_bytes,
            ending=ending if len(ending) <= self.max_line_bytes else b"",
            omitted_ending_bytes=len(ending)
            if len(ending) > self.max_line_bytes
            else 0,
        )
        if source_line_bytes + len(ending) <= self.max_line_bytes:
            return _FinishedEchoLine(line + finalization.ending, 0)
        settings = _EchoEncoding(encoding, errors, is_text_sink)

        retained, encoded = self._bounded_prefix(
            line,
            finalization=finalization,
            settings=settings,
        )
        dropped = finalization.source_bytes - len(retained)
        dropped += finalization.omitted_ending_bytes
        marker = truncation_marker(
            dropped,
            encoding=settings.encoding,
            errors=settings.errors,
        )
        remaining = self.max_line_bytes - len(encoded) - len(finalization.ending)
        marker = _fit_marker(
            marker,
            budget=max(remaining, 0),
            dropped=dropped,
            settings=settings,
        )
        return _FinishedEchoLine(encoded + marker + finalization.ending, dropped)

    def _bounded_prefix(
        self,
        line: bytes,
        *,
        finalization: _LineFinalization,
        settings: _EchoEncoding,
    ) -> tuple[bytes, bytes]:
        """Find a marker-consistent source prefix and its echoed bytes."""
        dropped = finalization.source_bytes + finalization.omitted_ending_bytes
        for _ in range(16):
            marker = truncation_marker(
                dropped,
                encoding=settings.encoding,
                errors=settings.errors,
            )
            budget = max(
                self.max_line_bytes - len(finalization.ending) - len(marker),
                0,
            )
            retained, encoded = _encode_prefix(
                line,
                budget=budget,
                settings=settings,
            )
            updated_dropped = finalization.source_bytes - len(retained)
            updated_dropped += finalization.omitted_ending_bytes
            if updated_dropped == dropped:
                return retained, encoded
            dropped = updated_dropped
        return retained, encoded


def _encode_prefix(
    line: bytes,
    *,
    budget: int,
    settings: _EchoEncoding,
) -> tuple[bytes, bytes]:
    """Return the largest source prefix whose echoed bytes fit *budget*."""
    if not settings.is_text_sink:
        prefix = line[:budget]
        return prefix, prefix

    decoder = codecs.getincrementaldecoder(settings.encoding)(
        errors=settings.errors,
    )
    encoder = codecs.getincrementalencoder(settings.encoding)(
        errors=settings.errors,
    )
    encoded_prefix = bytearray()
    best = (b"", b"")
    for length, byte in enumerate(line, start=1):
        try:
            decoded = decoder.decode(bytes((byte,)), final=False)
            encoded = encoder.encode(decoded, final=False)
        except UnicodeError:
            break
        encoded_prefix.extend(encoded)
        if decoder.getstate()[0]:
            continue
        if len(encoded_prefix) > budget:
            break
        best = (line[:length], bytes(encoded_prefix))
    return best


def _fit_marker(
    marker: bytes,
    *,
    budget: int,
    dropped: int,
    settings: _EchoEncoding,
) -> bytes:
    """Fit a marker into its remaining budget without splitting text encoding."""
    if len(marker) <= budget:
        return marker
    fallback = _ASCII_TRUNCATION_MARKER_TEMPLATE.format(dropped=dropped).encode(
        settings.encoding,
        settings.errors,
    )
    _source, encoded = _encode_prefix(
        fallback,
        budget=budget,
        settings=settings,
    )
    return encoded


def _split_echo_segments(
    chunk: bytes,
) -> list[tuple[bytes, bytes | None]]:
    r"""Split *chunk* into per-line echo writes for the bounded echo path.

    Parameters
    ----------
    chunk : bytes
        Raw bytes just read from the child stream.

    Returns
    -------
    list[tuple[bytes, bytes | None]]
        ``(body, ending)`` pairs in stream order, where ``body`` excludes its
        ``\\n`` terminator and ``ending`` is the raw line ending (``\\n`` or
        ``\\r\\n``). ``ending`` is ``None`` for the trailing pair when the
        chunk ends mid-line; its bytes still reach the limiter so a partial
        truncated line stays bounded before EOF. Unterminated bytes are
        re-emitted whole from the next chunk, so the limiter's own counters
        are the only cross-chunk state.
    """
    segments: list[tuple[bytes, bytes | None]] = []
    start = 0
    data = chunk
    while True:
        end = data.find(b"\n", start)
        if end == -1:
            break
        body = data[start:end]
        if body.endswith(b"\r"):
            segments.append((body[:-1], b"\r\n"))
        else:
            segments.append((body, b"\n"))
        start = end + 1
    if start < len(data):
        segments.append((data[start:], None))
    return segments
