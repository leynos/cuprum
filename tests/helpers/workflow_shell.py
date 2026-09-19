"""Shell-command matching and recipe reading for CI workflow contract tests.

Two related jobs live here. ``script_runs_command`` answers whether a workflow
step runs a command at all; the readers below answer what a step's script says
once it is known to be the right step — which shell function it declares, what
a flag is set to, and how a condition binds its operators. They share a home
because each of them is a question about shell text rather than about the
workflow that happens to carry it, and a contract test should not have to
reimplement shell reading to assert on a recipe.
"""

from __future__ import annotations

import dataclasses as dc
import re
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


def _shell_tokens(line: str, *, preserve_quotes: bool = False) -> list[str]:
    """Tokenize one shell line, optionally retaining quote delimiters."""
    lexer = shlex.shlex(line, posix=not preserve_quotes, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return list(lexer)


def _is_command_boundary(shell_word: str, *, is_command_position: bool) -> bool:
    """Return whether a shell word begins a new command segment."""
    return shell_word in _OPERATORS or (is_command_position and shell_word in _KEYWORDS)


def _quoted_heredoc_operator_indices(line: str, tokens: list[str]) -> frozenset[int]:
    """Return token positions whose ``<<`` spelling came from quoted text."""
    quoted_tokens = _shell_tokens(line, preserve_quotes=True)
    if len(tokens) != len(quoted_tokens):
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


@dc.dataclass(slots=True)
class _ScanState:
    """Running state of a top-level operator scan.

    Held as one object so a step can return the successor state rather than
    each field being threaded through the loop separately. The scan is a small
    state machine — inside or outside a quote, at some parenthesis depth — and
    naming that state is what keeps the loop a fold over characters instead of
    a branch per field.
    """

    depth: int = 0
    quote: str | None = None
    count: int = 0
    skip: int = 0


def _scan_char(
    expression: str,
    operator: str,
    index: int,
    state: _ScanState,
) -> _ScanState:
    """Return the scan state after consuming ``expression[index]``."""
    char = expression[index]
    if state.quote is not None:
        state.quote = None if char == state.quote else state.quote
        return state
    if char in "'\"":
        state.quote = char
        return state
    if char == "(":
        state.depth += 1
        return state
    if char == ")":
        state.depth -= 1
        return state
    if state.depth == 0 and expression.startswith(operator, index):
        state.count += 1
        state.skip = len(operator) - 1
    return state


def top_level_operators(expression: str, operator: str) -> int:
    """Count *operator* occurrences outside parentheses and quoted strings.

    `&&` and `||` are distinguished by where they bind, not merely by being
    present: a condition may legitimately parenthesize an inner disjunction
    while remaining conjunctive overall. Counting at depth zero is what makes
    "this guard is conjunctive" a statement about the whole condition rather
    than about whether the character pair appears anywhere in it.

    Parameters
    ----------
    expression : str
        The condition expression to scan.
    operator : str
        The operator to count, such as ``&&`` or ``||``.

    Returns
    -------
    int
        How many times *operator* binds at the top level.
    """
    state = _ScanState()
    index = 0
    while index < len(expression):
        state.skip = 0
        state = _scan_char(expression, operator, index, state)
        index += state.skip + 1
    return state.count


def shell_statements(body: str) -> tuple[str, ...]:
    """Return *body*'s statements, continuations joined and comments dropped.

    Comments are dropped before anything is matched, and the order matters:
    a workflow's own prose quotes the very command names and guards these
    readers search for, so a scan that ran over the commented body would find
    a guard in a comment and conclude the command was guarded. A comment cannot
    guard a command.

    Parameters
    ----------
    body : str
        The shell body to split into statements.

    Returns
    -------
    tuple[str, ...]
        One entry per statement, whitespace-collapsed and stripped.
    """
    uncommented = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    joined = re.sub(r"\\\n\s*", " ", uncommented)
    # Continuation folding leaves the joined line's indentation as runs of
    # spaces, so collapse them: callers assert which guard a statement carries,
    # not how it is laid out.
    collapsed = re.sub(r"[ \t]+", " ", joined)
    return tuple(line.strip() for line in collapsed.splitlines() if line.strip())


def shell_function(script: str, name: str, *, step: str) -> str:
    """Return the body of the shell function *name* declared in *script*.

    Parameters
    ----------
    script : str
        The step's script to search.
    name : str
        The shell function whose body is wanted.
    step : str
        The step's name, used only to say where the function was expected.

    Returns
    -------
    str
        The function's body, between its declaration and its closing brace.

    Raises
    ------
    AssertionError
        If *script* declares no such function.
    """
    match = re.search(
        rf"^[ \t]*{re.escape(name)}\(\)\s*\{{(?P<body>.*?)^[ \t]*\}}",
        script,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        msg = f"{name!r} must be declared as a shell function in {step!r}"
        raise AssertionError(msg)
    return match.group("body")


def flag_value(body: str, flag: str) -> str:
    """Return the value *body* passes to *flag*.

    Parameters
    ----------
    body : str
        The shell body to search.
    flag : str
        The flag whose argument value is wanted.

    Returns
    -------
    str
        The value written after *flag*.

    Raises
    ------
    AssertionError
        If *body* never passes *flag*.
    """
    match = re.search(
        rf"^[ \t]*{re.escape(flag)}[ \t]+(?P<value>[^\s\\]+)",
        body,
        re.MULTILINE,
    )
    if match is None:
        msg = f"the invocation must pass {flag} explicitly"
        raise AssertionError(msg)
    return match.group("value")
