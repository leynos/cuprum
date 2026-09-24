"""Recipe reading for CI workflow contract tests.

Once a contract test knows a step is the right one, these readers answer what
its script says: which shell function it declares, what a flag is set to, and
how a condition binds its operators. They share a home because each of them is
a question about shell text rather than about the workflow that happens to
carry it, and a contract test should not have to reimplement shell reading to
assert on a recipe.

Command *matching* — deciding whether a script runs a command at all — lives in
:mod:`tests.helpers.workflow_shell`, which is the module that owns tokenizing
and here-document tracking. These readers deliberately take an already-selected
body and never tokenize: they match on the body's text directly, which is what
keeps them total over the scripts a workflow actually writes.
"""

from __future__ import annotations

import dataclasses as dc
import re
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


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


def _starts_comment(line: str, index: int) -> bool:
    """Return whether ``line[index]`` begins a comment rather than a word."""
    return line[index] == "#" and (index == 0 or line[index - 1].isspace())


def _drop_comment(line: str) -> str:
    """Return *line* with any comment removed, quote-aware.

    A `#` inside a quoted string is part of that string, not the start of a
    comment, so the scan has to track quoting to find the real one. Workflow
    scripts quote command names and expressions freely, and truncating at the
    first `#` would silently shorten a statement a caller then asserts on.

    Parameters
    ----------
    line : str
        The line to strip a trailing comment from.

    Returns
    -------
    str
        The line up to its comment, or unchanged when it carries none.
    """
    quote: str | None = None
    for index, char in enumerate(line):
        if quote is not None:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif _starts_comment(line, index):
            return line[:index]
    return line


def _split_statements(body: str) -> cabc.Iterator[str]:
    """Yield *body*'s statements, split on newlines and unquoted `;`."""
    buffer: list[str] = []
    quote: str | None = None
    for char in body:
        if quote is not None:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in ";\n":
            yield "".join(buffer)
            buffer = []
            continue
        buffer.append(char)
    yield "".join(buffer)


def shell_statements(body: str) -> tuple[str, ...]:
    """Return *body*'s statements, continuations joined and comments dropped.

    A statement is one command and the guard attached to it, so the split has
    to happen at every separator the shell honours. Splitting on newlines alone
    is not enough: `make develop ...; true || return $?` is one physical line
    carrying an unguarded command followed by a guarded one, and a reader that
    did not split on the `;` would see a single statement ending in the guard
    and report the unguarded command as guarded.

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
    joined = re.sub(r"\\\n\s*", " ", body)
    uncommented = "\n".join(_drop_comment(line) for line in joined.splitlines())
    # Continuation folding leaves the joined line's indentation as runs of
    # spaces, so collapse them: callers assert which guard a statement carries,
    # not how it is laid out.
    collapsed = re.sub(r"[ \t]+", " ", uncommented)
    return tuple(
        statement.strip()
        for statement in _split_statements(collapsed)
        if statement.strip()
    )


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
