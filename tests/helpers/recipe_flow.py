"""Prove the suite recipe routes the selector's values into pytest.

`test_ci_suite_wiring_contract.py` asks whether anything *runs* the selector and
`tests/helpers/suite_selection.py` asks whether every module is *covered* by it.
Both answers are only as good as the recipe in between. A `test-python` target
that mentions `$(PYTEST_TARGETS)` inside an `echo`, or that iterates one
variable and then hands a different one to pytest, satisfies every
mentions-the-name check while collecting something else, or nothing at all.

Reading the recipe as text cannot tell those apart, so the claim is checked as a
**bounded** structural walk over the recipe's shell tokens. Bounded is the
operative word: this does not interpret the shell. It recognizes one shape —
the shape this repository's suite target is written in — and refuses the rest:

* a `for <name> in $(foreach <ignored>,<list>,<body>)` whose *list* argument is
  the selector, so the loop is driven by the selector rather than by a variable
  mentioned nearby;
* a `set -- <name>` inside that loop, so each iterated value becomes the
  positional parameters rather than being printed;
* a command whose program is the configured pytest invocation and whose
  arguments include the positional expansion, so those values reach pytest.

Anything else is refused by name, which is what makes the `echo`-only,
wrong-list, discarded-argument, and commented-out recipes fail here instead of
passing on their words. Refusing is the safe direction for a contract: a recipe
written in a different but equally valid shape is reported for a human to read
rather than silently certified.
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_documents import require
from tests.helpers.makefile import recipe_tokens

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("require_selector_drives_pytest", "require_selector_is_consumed")

#: Commands whose whole purpose is to print their arguments. A selector named
#: only among their arguments is reported to a reader, not handed to the suite,
#: so it does not count as consumed — which is the difference between a recipe
#: that runs the selection and one that describes it.
_PRINTERS = frozenset({"echo", "printf"})

#: A plain shell variable name, which is all `for` may bind here. A header whose
#: name is itself a Make expansion would mean the loop variable cannot be known
#: from the recipe text, so it is refused rather than guessed at.
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Tokens that end one shell command. `shlex` hands back `;`, `&&`, and `||`
#: both bare and glued to the preceding word, so a token is a separator when it
#: is one of these exactly or when it ends with `;`; the shell keywords are
#: included because they delimit the loop body the same way.
_COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|", "do", "done", "then", "fi"})

#: A leading `NAME=value` word: an environment assignment prefixed to a command.
#: Stripped before reading the command's program, so a recipe that sets
#: `RUSTFLAGS` before invoking pytest is read as invoking pytest.
_ENVIRONMENT_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")

#: How many `$(foreach` arguments to expect before treating the call as
#: unreadable. Three is the arity `make` defines.
_FOREACH_ARITY = 3

#: Tokens a loop header occupies before its `$(foreach` list: `for`, the
#: loop variable, `in`, and the call itself.
_LOOP_HEADER_WIDTH = 4


def _record(
    found: list[tuple[int, tuple[str, ...]]],
    current: list[str],
    start: int,
) -> None:
    """Append the command under construction, if it has any words.

    A separator at the start of a command, or two in a row, leaves ``current``
    empty; recording it would report a command the shell never ran, and the
    callers index into these words, so an empty one would read as a command
    with a program of ``""``.
    """
    if current:
        found.append((start, tuple(current)))


def _segments(
    tokens: cabc.Sequence[str],
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """Split tokens into commands, each with the index it started at.

    Parameters
    ----------
    tokens : Sequence of str
        The recipe's shell words, as :func:`recipe_tokens` returns them.

    Returns
    -------
    tuple of (int, tuple of str)
        One entry per command, in order, pairing the command's first token
        index with its words. The index is kept so a caller can ask whether one
        command runs before another without re-scanning the token list.
    """
    found: list[tuple[int, tuple[str, ...]]] = []
    current: list[str] = []
    start = 0
    for index, token in enumerate(tokens):
        if token in _COMMAND_SEPARATORS:
            _record(found, current, start)
            current = []
            start = index + 1
            continue
        if token.endswith(";"):
            current.append(token[:-1])
            _record(found, current, start)
            current = []
            start = index + 1
            continue
        current.append(token)
    _record(found, current, start)
    return tuple(found)


def _program(words: cabc.Sequence[str]) -> str:
    """Return a command's program, ignoring leading environment assignments."""
    index = 0
    while index < len(words) and _ENVIRONMENT_ASSIGNMENT.match(words[index]):
        index += 1
    return words[index] if index < len(words) else ""


def _loop_header(
    tokens: cabc.Sequence[str],
    *,
    selector: str,
) -> tuple[int, str]:
    """Return the index and variable of the loop the selector drives.

    Parameters
    ----------
    tokens : Sequence of str
        The recipe's shell words.
    selector : str
        The Makefile variable the loop must iterate.

    Returns
    -------
    tuple of (int, str)
        The token index of the `$(foreach` call, and the shell variable the
        loop binds.

    Raises
    ------
    AssertionError
        If no `for` loop iterates the selector, or if the loop that comes
        closest iterates some other variable. The message names the list
        argument it read, so a recipe feeding the loop from the wrong variable
        is reported as that rather than as a missing loop.
    """
    reference = f"$({selector})"
    # `for` must be the command's *program*, not a word inside one: an
    # `echo for p in $(foreach …` carries every token the loop does, and the
    # shell would run `echo`. Reading the segments rather than the flat token
    # list is what keeps an argument from being mistaken for the loop itself.
    for index, words in _segments(tokens):
        if _program(words) != "for":
            continue
        window = tokens[index : index + _LOOP_HEADER_WIDTH]
        if len(window) < _LOOP_HEADER_WIDTH or window[2:4] != ("in", "$(foreach"):
            continue
        variable = window[1]
        if not _VARIABLE_NAME.fullmatch(variable):
            continue
        head = " ".join(tokens[index + 3 :]).split(";", 1)[0]
        pieces = head.split(",", _FOREACH_ARITY - 1)
        require(
            condition=len(pieces) >= _FOREACH_ARITY,
            message=(
                f"the `test-python` recipe's `$(foreach` call does not carry "
                f"the three arguments `make` defines, so the list it iterates "
                f"cannot be read. Read: {head!r}"
            ),
        )
        iterated = pieces[1].strip()
        require(
            condition=reference in iterated,
            message=(
                f"the `test-python` recipe iterates {iterated!r} rather than "
                f"{reference}, so the selector is not the list the loop is "
                "driven by; it is merely a variable the recipe mentions"
            ),
        )
        return index, variable
    require(
        condition=False,
        message=(
            f"the `test-python` recipe must iterate {reference} in a "
            f"`$(foreach`, so the selector supplies the loop rather than "
            "appearing somewhere the shell never evaluates it"
        ),
    )
    raise AssertionError


def _positional_binding(
    tokens: cabc.Sequence[str],
    *,
    variable: str,
    after: int,
) -> int:
    """Return the index of the `set --` that binds the loop variable.

    Parameters
    ----------
    tokens : Sequence of str
        The recipe's shell words.
    variable : str
        The shell variable the loop binds.
    after : int
        Index of the loop header, so a `set --` before the loop is not read as
        the loop body's binding.

    Returns
    -------
    int
        The token index of the `set` keyword.

    Raises
    ------
    AssertionError
        If no `set --` binds the loop variable after the loop begins. Without
        it the iterated value is not routed into the positional parameters at
        all, so whatever pytest receives did not come from the selector.
    """
    spellings = {f"$${variable}", f"$${{{variable}}}"}
    for index in range(after, len(tokens) - 2):
        if tokens[index] != "set" or tokens[index + 1] != "--":
            continue
        if tokens[index + 2].rstrip(";") in spellings:
            return index
    require(
        condition=False,
        message=(
            f"the `test-python` recipe's loop must bind its value with "
            f"`set -- $${variable}`, so each iterated pattern becomes the "
            "positional parameters; without it the loop value is not routed "
            "to the command that runs pytest"
        ),
    )
    raise AssertionError


def _pytest_command(
    tokens: cabc.Sequence[str],
    *,
    pytest_variable: str,
    after: int,
) -> tuple[int, tuple[str, ...]]:
    """Return the pytest invocation that consumes the positional parameters.

    Parameters
    ----------
    tokens : Sequence of str
        The recipe's shell words.
    pytest_variable : str
        The Makefile variable naming the pytest command.
    after : int
        Index of the `set --` binding, so a pytest invocation outside the loop
        body is not read as consuming what the loop bound.

    Returns
    -------
    tuple of (int, tuple of str)
        The command's first token index and its words.

    Raises
    ------
    AssertionError
        If no command in the loop body invokes pytest with the positional
        expansion. A command that runs pytest over something else, or that
        expands the positional parameters in some *other* command, does not
        deliver the selector's values to the test run.
    """
    program = f"$({pytest_variable})"
    for start, words in _segments(tokens):
        if start <= after or _program(words) != program:
            continue
        require(
            condition="$$@" in words,
            message=(
                f"the `test-python` recipe's `{program}` command does not "
                "expand `$$@`, so the patterns the loop bound are discarded "
                "rather than passed to pytest"
            ),
        )
        return start, words
    require(
        condition=False,
        message=(
            f"the `test-python` recipe must invoke {program} inside the loop, "
            "after the patterns are bound, so the selector's values reach the "
            "pytest run rather than a command beside it"
        ),
    )
    raise AssertionError


def require_selector_drives_pytest(
    recipe: str,
    *,
    selector: str,
    pytest_variable: str,
) -> None:
    """Require a recipe to carry the selector's values into the pytest run.

    Parameters
    ----------
    recipe : str
        Recipe text, as `recipe_of` returns it.
    selector : str
        The Makefile variable holding the pattern list the suite iterates.
    pytest_variable : str
        The Makefile variable naming the pytest invocation.

    Raises
    ------
    AssertionError
        If the recipe does not iterate the selector, does not bind each value
        to the positional parameters, or does not pass those parameters to
        pytest. Each message names the missing step, so a broken recipe is
        reported as the step it lost rather than as a generic failure.

    Examples
    --------
    >>> require_selector_drives_pytest(
    ...     "for p in $(foreach t,$(S),$(t)); do set -- $$p; $(PY) $$@; done",
    ...     selector="S",
    ...     pytest_variable="PY",
    ... )
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    tokens = recipe_tokens(recipe)
    loop, variable = _loop_header(tokens, selector=selector)
    binding = _positional_binding(tokens, variable=variable, after=loop)
    _pytest_command(tokens, pytest_variable=pytest_variable, after=binding)


def require_selector_is_consumed(recipe: str, *, selector: str) -> None:
    """Require a recipe to hand the selector to a command it actually runs.

    Weaker than :func:`require_selector_drives_pytest`, and used where only the
    weaker claim is available. An exemption pairs a selector with the target
    that is supposed to run it, but that target may be any recipe in the
    Makefile, so the full loop shape cannot be required of it. What must hold
    either way is that the selector reaches a command the shell executes.

    Two ways of naming a selector without consuming it are refused. Text the
    shell drops — a commented-out entry — contributes no token at all, so it is
    caught by tokenizing rather than by searching the recipe string. Text a
    command merely *prints* is live, so it is caught separately: `echo
    '$(PYTEST_TARGETS)'` runs, and describes the selector to a reader rather
    than passing it to anything.

    Parameters
    ----------
    recipe : str
        Recipe text, as `recipe_of` returns it.
    selector : str
        The Makefile variable the target must consume.

    Raises
    ------
    AssertionError
        If the selector appears only in dropped text, or only among the
        arguments of a command that prints them.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    reference = f"$({selector})"
    named = [
        words
        for _start, words in _segments(recipe_tokens(recipe))
        if any(reference in word for word in words)
    ]
    require(
        condition=bool(named),
        message=(
            f"the target's recipe does not expand {reference} in a command the "
            "shell would run, so the selector names nothing the target executes"
        ),
    )
    require(
        condition=any(_program(words) not in _PRINTERS for words in named),
        message=(
            f"the target's recipe names {reference} only among the arguments "
            "of a command that prints them, so the selector is described "
            "rather than consumed; nothing the target runs uses it"
        ),
    )
