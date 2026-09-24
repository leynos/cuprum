"""Shell-command *matching* for CI workflow contract tests.

``script_runs_command`` answers whether a workflow step runs a command at all.
It owns the tokenizing and here-document tracking that question needs, because
answering it means reading a whole script rather than a body already known to
be the right one.

Reading *what* a step's script says — which shell function it declares, what a
flag is set to, how a condition binds its operators — lives in
:mod:`tests.helpers.workflow_recipe`. The two modules split on that boundary so
each stays within the line budget ``AGENTS.md`` sets and the lint gate enforces,
and so the matcher's token-level machinery is not pulled into readers that never
tokenize.
"""

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

    Parentheses inside a quoted command substitution confuse this mode.
    Retry without parenthesis punctuation in that case; callers still require
    positional agreement with the POSIX pass before trusting the result.

    Returns
    -------
    list[str] | None
        One entry per shell word, with any quote delimiters retained, or
        ``None`` if the line cannot be lexed in this mode.
    """
    for punctuation in (True, ";&|<>"):
        try:
            lexer = shlex.shlex(line, posix=False, punctuation_chars=punctuation)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            return list(lexer)
        except ValueError:
            continue
    return None


def _is_command_boundary(shell_word: str, *, is_command_position: bool) -> bool:
    """Return whether a shell word begins a new command segment."""
    return shell_word in _OPERATORS or (is_command_position and shell_word in _KEYWORDS)


def _quoted_heredoc_operator_indices(
    line: str, tokens: list[str]
) -> frozenset[int] | None:
    """Return token positions whose ``<<`` spelling came from quoted text.

    Keep failed analysis distinct from a confirmed absence of quoted operators.
    The caller must refuse an ambiguous redirect rather than treat it as a
    real here-document and silently hide subsequent commands.

    Returns
    -------
    frozenset[int] | None
        Confirmed quoted positions, or ``None`` when token alignment is unknown.
    """
    quoted_tokens = _quoted_shell_tokens(line)
    if quoted_tokens is None or len(tokens) != len(quoted_tokens):
        return None
    return frozenset(
        index
        for index, (shell_word, quoted_shell_word) in enumerate(
            zip(tokens, quoted_tokens, strict=True)
        )
        if shell_word == "<<" and quoted_shell_word != "<<"
    )


def _here_document_delimiters(
    tokens: list[str], quoted_operator_indices: frozenset[int] | None
) -> cabc.Iterator[str]:
    """Yield confirmed here-document delimiters in declaration order.

    Yields
    ------
    str
        Delimiters whose redirect operators are confirmed unquoted.

    Raises
    ------
    ValueError
        If redirect quoting cannot be classified reliably.
    """
    if "<<" not in tokens:
        return
    if quoted_operator_indices is None:
        message = "cannot classify here-document quoting"
        raise ValueError(message)
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
        If quoting is unclosed or a redirect's quoting cannot be classified.
    """  # ruff: ignore[docstring-extraneous-exception] - shlex propagates malformed quoting.
    expected = tuple(shlex.split(command))
    return any(
        _segment_starts_command(segment, expected)
        for segment in _command_segments(script)
    )
