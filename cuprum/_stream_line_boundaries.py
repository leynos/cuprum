"""Split decoded stream text into lines without rescanning retained text.

:func:`_split_complete_lines` serves callers that already hold a complete
string: it is a pure function over the whole text. The stream drain cannot use
it, because it sees the text one chunk at a time: concatenating each chunk onto
a growing pending string and rescanning it copies and searches everything
before it on every read, so a single newline-free line costs quadratic work and
retains the whole line in memory. :class:`_LineAccumulator` replaces that with
one linear pass over each chunk and a bounded pending buffer, which is what
keeps an attacker-influenced child from exhausting the parent.
"""

from __future__ import annotations

import collections as co
import re

_LINE_BOUNDARY_CHARACTERS = (
    "\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
)

# One alternation-free character class, compiled once. The order of the class
# mirrors ``_LINE_BOUNDARY_CHARACTERS`` for readability only; a character class
# has no alternation semantics to get wrong.
_LINE_BOUNDARY_PATTERN = re.compile(
    "[" + "".join(re.escape(char) for char in _LINE_BOUNDARY_CHARACTERS) + "]",
)


def _split_complete_lines(
    text: str,
    *,
    final: bool = True,
) -> tuple[list[str], str]:
    """Split text into completed lines and a trailing partial line.

    Parameters
    ----------
    text : str
        Text to split using Python's universal line boundary rules.
    final : bool
        Whether no more decoded text will arrive. A non-final trailing carriage
        return remains pending because it may prefix a following line feed.

    Returns
    -------
    tuple[list[str], str]
        Completed lines with one trailing line ending removed from each line,
        followed by the remaining partial line. The remainder is empty when
        ``text`` ends with a line ending or contains no partial line. A
        non-final trailing carriage return remains pending.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return [], text

    remainder = ""
    if _should_hold_trailing_line(lines[-1], final=final):
        remainder = lines.pop()

    return [_strip_line_ending(line) for line in lines], remainder


def _find_next_boundary(
    text: str,
    start: int,
    /,
) -> re.Match[str] | None:
    """Return the first line boundary at or after ``start``, if any."""
    return _LINE_BOUNDARY_PATTERN.search(text, start)


class _LineAccumulator:
    """Incrementally assemble lines from decoded chunks in linear time.

    The accumulator keeps pending content as the list of chunks it arrived in,
    so appending a chunk never copies the text already retained. A search for
    the next boundary only ever covers the new chunk, and once a boundary is
    found the completed line is assembled once. Total work is therefore linear
    in the decoded text rather than quadratic in the length of the longest
    line.

    ``max_line_chars`` bounds how much *content* may be retained for one
    incomplete line. While the pending count exceeds the bound, the leading
    ``max_line_chars`` characters are emitted as a fragment and the rest is
    kept. A fragment is an ordinary emitted line as far as the sink is
    concerned: it travels through the same awaited sink, so a fragmenting run
    still honours backpressure. Line terminators are never counted or
    retained, so a line of exactly ``max_line_chars`` characters followed by a
    terminator emits one event, not a fragment plus an empty one. ``None``
    disables the bound and restores unbounded retention for callers who have
    made their own arrangement.

    Parameters
    ----------
    max_line_chars : int | None
        Maximum number of decoded characters to retain for one line or
        fragment, or ``None`` for no bound.
    """

    def __init__(self, max_line_chars: int | None) -> None:
        """Start with no pending text and no held boundary."""
        self._parts: co.deque[str] = co.deque()
        self._pending_chars = 0
        self._skip_next_lf = False
        self._max_line_chars = max_line_chars

    @property
    def pending_chars(self) -> int:
        """How many decoded characters await a line boundary."""
        return self._pending_chars

    def feed(self, text: str) -> list[str]:
        """Consume decoded text and return the lines and fragments it completes.

        Parameters
        ----------
        text : str
            Newly decoded text. It may end mid-line or mid-CRLF; both are
            carried into the next call.

        Returns
        -------
        list[str]
            Completed lines and overflow fragments, in stream order. A line
            may be empty, because ``str.splitlines()`` treats a bare boundary
            as one empty line.
        """
        if not text:
            return []

        emitted: list[str] = []
        start = 0
        if self._skip_next_lf:
            # The previous chunk ended in a lone carriage return. If this
            # chunk starts with a line feed the two form one CRLF terminator,
            # so the feed must be consumed before it is allowed to complete
            # another (empty) line of its own.
            if text[0] == "\n":
                start = 1
            self._skip_next_lf = False
            emitted.extend(self._complete_line())

        while True:
            boundary = _find_next_boundary(text, start)
            if boundary is None:
                self._append(text[start:])
                # A long newline-free run must not wait for its terminator to
                # be bounded: emit fragments as they fill.
                emitted.extend(self._split_overflow())
                return emitted

            index = boundary.start()
            char = text[index]
            if char == "\r" and index + 1 == len(text):
                # The carriage return may pair with a line feed in the next
                # chunk, so the line is not complete yet.
                self._append(text[start:index])
                self._skip_next_lf = True
                emitted.extend(self._split_overflow())
                return emitted

            width = 2 if char == "\r" and text[index + 1] == "\n" else 1
            self._append(text[start:index])
            emitted.extend(self._complete_line())
            start = index + width

    def finish(self) -> str | None:
        """Return the trailing line once no more decoded text will arrive.

        Returns
        -------
        str | None
            The final line or fragment, which may be empty when the stream
            ended in a terminator that completed an empty line. ``None`` when
            the stream ended on a boundary with nothing pending.
        """
        held_boundary = self._skip_next_lf
        self._skip_next_lf = False
        if not held_boundary and not self._parts:
            return None
        return self._take(self._pending_chars)

    def _append(self, text: str) -> None:
        """Add text to the pending line without copying what is retained."""
        if text:
            self._parts.append(text)
            self._pending_chars += len(text)

    def _split_overflow(self) -> list[str]:
        """Return fragments that keep the pending line within the bound."""
        bound = self._max_line_chars
        if bound is None or self._pending_chars <= bound:
            return []

        # Join once and slice once. Taking each fragment with _take would copy
        # the remaining head again for every fragment, which is quadratic in a
        # single large chunk when the bound is small. The retained text is at
        # most one bound plus the newest chunk, so this stays linear.
        pending = "".join(self._parts)
        fragment_count = (len(pending) - 1) // bound
        fragments = [
            pending[index * bound : (index + 1) * bound]
            for index in range(fragment_count)
        ]
        remainder = pending[fragment_count * bound :]
        self._parts.clear()
        if remainder:
            self._parts.append(remainder)
        self._pending_chars = len(remainder)
        return fragments

    def _complete_line(self) -> list[str]:
        """Return any overflow fragments, then the completed line itself."""
        fragments = self._split_overflow()
        fragments.append(self._take(self._pending_chars))
        return fragments

    def _take(self, count: int) -> str:
        """Remove and return the leading ``count`` pending characters."""
        taken: list[str] = []
        remaining = count
        while remaining and self._parts:
            part = self._parts[0]
            if len(part) <= remaining:
                taken.append(self._parts.popleft())
                remaining -= len(part)
            else:
                taken.append(part[:remaining])
                self._parts[0] = part[remaining:]
                remaining = 0
        text = "".join(taken)
        self._pending_chars -= len(text)
        return text


def _should_hold_trailing_line(line: str, *, final: bool) -> bool:
    """Return whether a trailing line needs the next decoded chunk."""
    if not _ends_with_line_ending(line):
        return True
    return not final and line.endswith("\r")


def _ends_with_line_ending(line: str) -> bool:
    """Return whether ``line`` ends with a Python-recognized line boundary."""
    return line.endswith(_LINE_BOUNDARY_CHARACTERS)


def _strip_line_ending(line: str) -> str:
    r"""Strip one trailing ``str.splitlines()`` boundary from ``line``."""
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(_LINE_BOUNDARY_CHARACTERS):
        return line[:-1]
    return line


__all__ = ["_LineAccumulator", "_split_complete_lines", "_strip_line_ending"]
