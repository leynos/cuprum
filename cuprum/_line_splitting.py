"""Superseded text-level line splitting retained as a small pure helper.

The stream drain no longer calls this module. Decoded chunks now travel
through :class:`cuprum._stream_line_boundaries._LineAccumulator`, which scans
each chunk once and bounds the text retained for an unterminated line; the
pure splitting below could only be used by first concatenating every pending
chunk onto the text being split, which is the quadratic, unbounded behaviour
issue #532 removed. The rules here are kept as the readable statement of the
CR, LF, and CRLF contract, and to avoid a module deletion mixed into a
behavioural fix.
"""

from __future__ import annotations


def _split_complete_lines(text: str) -> tuple[list[str], str]:
    r"""Split text into completed lines and a trailing partial line.

    Parameters
    ----------
    text : str
        Text to split using CR, LF, and CRLF line boundaries.

    Returns
    -------
    tuple[list[str], str]
        Completed lines with one trailing line ending removed from each line,
        followed by the remaining partial line. A terminal carriage return is
        retained until a later chunk can determine whether it begins ``\r\n``.
    """
    lines: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        character = text[index]
        match character:
            case "\n":
                lines.append(text[start:index])
                start = index + 1
            case "\r":
                if index + 1 == len(text):
                    return lines, text[start:]
                lines.append(text[start:index])
                if text[index + 1] == "\n":
                    index += 1
                start = index + 1
        index += 1
    return lines, text[start:]


def _strip_line_ending(line: str) -> str:
    r"""Strip a single trailing ``\r\n``, ``\n``, or ``\r`` from ``line``."""
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(("\n", "\r")):
        return line[:-1]
    return line
