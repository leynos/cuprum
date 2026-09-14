"""Text-level line splitting shared by the stream-drain loop.

The drain loop emits decoded text to line observers in complete lines and
mirrors each line's terminator handling, so the rules for recognizing and
stripping line endings live in one pure module both the loop and its tests
can depend on.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _emit_completed_lines(
    text: str,
    *,
    on_line: cabc.Callable[[str], None],
) -> str:
    """Emit complete lines from text and return the remaining partial line."""
    lines, remainder = _split_complete_lines(text)

    for line in lines:
        on_line(line)

    return remainder


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


def _ends_with_line_ending(line: str) -> bool:
    """Return whether ``line`` ends with a newline or carriage return."""
    return line.endswith(("\n", "\r"))


def _strip_line_ending(line: str) -> str:
    r"""Strip a single trailing ``\r\n``, ``\n``, or ``\r`` from ``line``."""
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(("\n", "\r")):
        return line[:-1]
    return line
