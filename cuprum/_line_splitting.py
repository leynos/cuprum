"""Text-level line splitting shared by the stream-drain loop.

The drain loop emits decoded text to line observers in complete lines and
mirrors each line's terminator handling, so the rules for recognizing and
stripping line endings live in one pure module both the loop and its tests
can depend on.

Only the splitting rules live here. Emitting the split lines is the drain
loop's job and awaits each sink so a slow consumer can apply backpressure, so
``cuprum._streams`` owns that half (``_emit_completed_lines``).
"""

from __future__ import annotations


def _split_complete_lines(text: str) -> tuple[list[str], str]:
    r"""Split text into completed lines and a trailing partial line.

    Parameters
    ----------
    text : str
        Text to split using Python's universal line boundary rules.

    Returns
    -------
    tuple[list[str], str]
        Completed lines with one trailing line ending removed from each line,
        followed by the remaining partial line. A terminal carriage return is
        retained until a later chunk can determine whether it begins ``\r\n``.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return [], text

    remainder = ""
    if _should_hold_trailing_line(lines[-1]):
        remainder = lines.pop()

    return [_strip_line_ending(line) for line in lines], remainder


def _should_hold_trailing_line(line: str) -> bool:
    """Return whether a trailing line needs the next decoded chunk."""
    return not _ends_with_line_ending(line) or line.endswith("\r")


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
