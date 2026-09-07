"""Split decoded stream text without breaking CRLF pairs across reads."""

from __future__ import annotations

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


__all__ = ["_split_complete_lines", "_strip_line_ending"]
