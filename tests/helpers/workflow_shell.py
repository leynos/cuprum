"""Shell-command matching used by CI workflow contract tests."""

from __future__ import annotations

import shlex
import typing as typ
from collections import deque

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _is_environment_assignment(token: str) -> bool:
    """Return whether ``token`` is a leading shell environment assignment."""
    if "=" not in token:
        return False
    name, _ = token.split("=", maxsplit=1)
    return name.isidentifier()


_OPERATORS = frozenset({"&", "&&", ";", "|", "||"})
_KEYWORDS = frozenset({"if", "then", "elif", "else", "do"})


def _shell_tokens(line: str) -> list[str]:
    """Tokenize one shell line."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return list(lexer)


def _is_command_boundary(shell_word: str, *, is_command_position: bool) -> bool:
    """Return whether a shell word begins a new command segment."""
    return shell_word in _OPERATORS or (is_command_position and shell_word in _KEYWORDS)


def _here_document_delimiters(tokens: list[str]) -> cabc.Iterator[str]:
    """Yield declared here-document delimiters in declaration order."""
    for index, shell_word in enumerate(tokens[:-1]):
        if shell_word == "<<":
            yield tokens[index + 1]


def _consume_here_document_line(line: str, delimiter: str) -> str | None:
    """Return the delimiter while a here-document body remains."""
    terminator = delimiter.removeprefix("-")
    candidate = line.lstrip("\t") if delimiter.startswith("-") else line
    return None if candidate == terminator else delimiter


def _command_segments_from_tokens(tokens: list[str]) -> cabc.Iterator[list[str]]:
    """Yield command segments from a shell line's tokens."""
    segment: list[str] = []
    is_command_position = True
    for token in [*tokens, ";"]:
        if _is_command_boundary(token, is_command_position=is_command_position):
            yield segment
            segment = []
            is_command_position = True
            continue
        segment.append(token)
        is_command_position = False


def _command_segments(script: str) -> cabc.Iterator[list[str]]:
    """Yield shell-token segments split at command boundaries."""
    here_document_delimiters: deque[str] = deque()
    for line in script.replace("\\\n", " ").splitlines():
        if here_document_delimiters:
            if _consume_here_document_line(line, here_document_delimiters[0]) is None:
                here_document_delimiters.popleft()
            continue
        tokens = _shell_tokens(line)
        yield from _command_segments_from_tokens(tokens)
        here_document_delimiters.extend(_here_document_delimiters(tokens))


def _segment_starts_command(segment: list[str], expected: tuple[str, ...]) -> bool:
    """Return whether a shell segment starts with the expected command."""
    while segment and _is_environment_assignment(segment[0]):
        segment.pop(0)
    return tuple(segment[: len(expected)]) == expected


def script_runs_command(script: str, command: str) -> bool:
    """Return whether ``script`` executes ``command`` as leading shell tokens.

    Parameters
    ----------
    script : str
        Shell script to inspect.
    command : str
        Command whose token sequence must begin a script segment.

    Returns
    -------
    bool
        Whether a command segment in ``script`` starts with ``command``, after
        leading environment assignments have been ignored.
    """
    expected = tuple(shlex.split(command))
    return any(
        _segment_starts_command(segment, expected)
        for segment in _command_segments(script)
    )
