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
    """Tokenize one shell line into shell words, splitting operators."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return list(lexer)


def _quoted_shell_tokens(line: str) -> list[str] | None:
    """Tokenize one shell line retaining quote delimiters, for comparison.

    Only ever compared with :func:`_shell_tokens`, never executed, so the
    tokens need only line up positionally with that pass. This one is
    deliberately non-``posix`` because that is the mode that preserves the
    quote delimiters the comparison keys on. It must still split punctuation,
    though: the two passes are compared index by index, which is only
    meaningful if a shell operator lands at the same position in both.
    Without ``punctuation_chars`` the line ``echo "<<"&&true`` tokenizes as
    three words here and four in the other pass, the counts disagree, and a
    caller gives up on a line it could have read — misreading the quoted
    ``<<`` as a here-document operator, which then swallows every following
    line.

    That combination cannot lex a double-quoted command substitution:
    ``payload="$(mktemp)"`` raises ``ValueError: No closing quotation``. Such
    an assignment is unremarkable — ``ci.yml`` writes three of them — so
    rather than let one take out every caller that scans a job, an unlexable
    line returns ``None``, which callers report as "cannot be compared".

    Returns
    -------
    list[str] | None
        One entry per shell word, with any quote delimiters retained, or
        ``None`` if the line cannot be lexed in this mode.
    """
    try:
        lexer = shlex.shlex(line, posix=False, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        return list(lexer)
    except ValueError:
        return None


def _is_command_boundary(shell_word: str, *, is_command_position: bool) -> bool:
    """Return whether a shell word begins a new command segment."""
    return shell_word in _OPERATORS or (is_command_position and shell_word in _KEYWORDS)


def _quoted_heredoc_operator_indices(line: str, tokens: list[str]) -> frozenset[int]:
    """Return token positions whose ``<<`` spelling came from quoted text.

    A line that cannot be lexed in the comparison mode, or that the two passes
    split differently, yields no indices: the line is reported as "cannot be
    compared" rather than guessed at. No line in this repository's workflows
    currently reaches either branch with a quoted ``<<`` on it, so the
    fallback costs nothing today; it exists so that the failure is a
    conservative miss rather than an invented here-document.

    Returns
    -------
    frozenset[int]
        Positions confirmed to contain quoted operators.
    """
    quoted_tokens = _quoted_shell_tokens(line)
    if quoted_tokens is None or len(tokens) != len(quoted_tokens):
        return frozenset()
    return frozenset(
        index
        for index, (shell_word, quoted_shell_word) in enumerate(
            zip(tokens, quoted_tokens, strict=True)
        )
        if shell_word == "<<" and quoted_shell_word != "<<"
    )


def _here_document_delimiters(
    tokens: list[str], quoted_operator_indices: frozenset[int]
) -> cabc.Iterator[str]:
    """Yield declared here-document delimiters in declaration order."""
    for index, shell_word in enumerate(tokens[:-1]):
        if shell_word == "<<" and index not in quoted_operator_indices:
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
        here_document_delimiters.extend(
            _here_document_delimiters(
                tokens, _quoted_heredoc_operator_indices(line, tokens)
            )
        )


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

    Raises
    ------
    ValueError
        If ``script`` or ``command`` contains unclosed shell quoting.
    """  # ruff: ignore[docstring-extraneous-exception] - shlex propagates malformed quoting.
    expected = tuple(shlex.split(command))
    return any(
        _segment_starts_command(segment, expected)
        for segment in _command_segments(script)
    )
