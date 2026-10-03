"""Prove the suite recipe routes the selector's values into pytest.

`test_ci_suite_wiring_contract.py` asks whether anything *runs* the selector and
`tests/helpers/suite_selection.py` asks whether every module is *covered* by it.
Both answers are only as good as the recipe in between: a `test-python` target
that mentions `$(PYTEST_TARGETS)` inside an `echo`, or that iterates one
variable and hands a different one to pytest, satisfies every
mentions-the-name check while collecting something else, or nothing at all.
Reading the recipe as text cannot tell those apart, so the claim is checked as a
**bounded** structural walk over the recipe's shell tokens, never a shell
interpreter. It recognizes the shape this repository's suite target is written
in — a `for` loop over the selector, whose body binds each value with `set --`
and hands it to the configured pytest command — and refuses everything else by
name, which is what makes the `echo`-only, wrong-list, discarded-argument, and
commented-out recipes fail here instead of passing on their words.

Refusing is the safe direction for a contract: a recipe in a different but
equally valid shape is reported for a human to read rather than silently
certified.
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_documents import require
from tests.helpers.recipe_read import (
    command_program,
    command_segments,
    recipe_tokens,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "require_module_runs_under_pytest",
    "require_selector_drives_pytest",
    "require_selector_is_consumed",
)

#: Commands whose whole purpose is to print their arguments. A selector named
#: only among them describes the selection rather than running it, so it is not
#: counted as consumed.
_PRINTERS = frozenset({"echo", "printf"})

#: A plain shell variable name, all `for` may bind here: a name that is itself a
#: Make expansion cannot be known from the recipe text, so it is refused.
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: How many `$(foreach` arguments to expect before treating the call as
#: unreadable. Three is the arity `make` defines.
_FOREACH_ARITY = 3

#: Tokens a loop header occupies before its `$(foreach` list: `for`, the loop
#: variable, `in`, and the call itself.
_LOOP_HEADER_WIDTH = 4

#: The word at which `set -- <value>` carries the value it binds: `set`, the
#: `--` separator, then the value itself.
_SET_VALUE_INDEX = 2


def _loop_shape(tokens: cabc.Sequence[str], index: int) -> tuple[str, str, int] | None:
    """Return a `for` header's variable, list, and `done`, else `None`.

    `None` is how a caller tells the loop from a command that merely carries
    its words: an `echo for p in $(foreach …` has every token the loop does
    while the shell runs `echo`. The `done` is sought in the token stream,
    because `command_segments` consumes it as the separator it is, so it is never a
    command's program. A nested loop would end the body early; this suite
    target has none, and generalizing would buy a shell interpreter.
    """  # ruff: ignore[docstring-missing-returns] - the summary names the return
    window = tokens[index : index + _LOOP_HEADER_WIDTH]
    # A short window yields a short slice, which is not the pair sought — so
    # the length check is the comparison rather than a clause beside it.
    if tuple(window[2:4]) != ("in", "$(foreach"):
        return None
    variable = window[1]
    if not _VARIABLE_NAME.fullmatch(variable):
        return None
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
    body_end = next(
        (
            position
            for position, token in enumerate(tokens)
            if position > index and token.rstrip(";") == "done"
        ),
        -1,
    )
    require(
        condition=body_end >= 0,
        message=(
            "the `test-python` recipe's `for` loop is not closed by a "
            "`done`, so the loop body has no end and the recipe cannot run "
            "as written"
        ),
    )
    return variable, pieces[1].strip(), body_end


def _loop_header(
    tokens: cabc.Sequence[str],
    *,
    selector: str,
) -> tuple[int, str, int]:
    """Return the loop the selector drives: its index, variable, and `done`.

    Raises `AssertionError` when no `for` loop iterates the selector, or the
    closest one iterates another variable — reported by naming the list read.
    """  # ruff: ignore[docstring-missing-returns, docstring-missing-exception] - the summary names the return and the refusal
    reference = f"$({selector})"
    for index, words in command_segments(tokens):
        if command_program(words) != "for":
            continue
        shape = _loop_shape(tokens, index)
        if shape is None:
            continue
        variable, iterated, body_end = shape
        require(
            condition=reference in iterated,
            message=(
                f"the `test-python` recipe iterates {iterated!r} rather than "
                f"{reference}, so the selector is not the list the loop is "
                "driven by; it is merely a variable the recipe mentions"
            ),
        )
        return index, variable, body_end
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
    before: int,
) -> int:
    """Return the index of the `set --` the pytest command actually consumes.

    That is the *last* `set` between the loop header and the pytest invocation.
    The positional parameters are mutable state, so an earlier binding says
    nothing about what pytest receives: `set -- $$p; set -- other.py; $(PYTEST)
    $$@` binds the iterated pattern and then discards it, and a check reading
    the first binding would certify that recipe. Reading the last one is what
    makes the value pytest consumes the value the loop iterated.

    The binding must be a *command* — so `echo set -- $$p` is not one. Raises
    `AssertionError` when nothing in the span binds, or when the last binding
    is not the loop variable's.
    """  # ruff: ignore[docstring-missing-returns] - the summary names the return
    bindings = [
        (start, words)
        for start, words in command_segments(tokens)
        if after < start < before and command_program(words) == "set"
    ]
    require(
        condition=bool(bindings),
        message=(
            f"the `test-python` recipe's loop must bind its value with "
            f"`set -- $${variable}`, so each iterated pattern becomes the "
            "positional parameters; without it the loop value is not routed "
            "to the command that runs pytest"
        ),
    )
    start, words = bindings[-1]
    spellings = {f"$${variable}", f"$${{{variable}}}"}
    require(
        condition=len(words) > _SET_VALUE_INDEX
        and words[_SET_VALUE_INDEX].rstrip(";") in spellings,
        message=(
            f"the `test-python` recipe's loop binds $${variable}, then "
            "overwrites the positional parameters with a later `set` before "
            "invoking pytest, so the patterns the loop iterated are discarded "
            "rather than passed to it"
        ),
    )
    return start


def _pytest_command(
    tokens: cabc.Sequence[str], *, pytest_variable: str, after: int, body_end: int
) -> int:
    """Return the index of the pytest invocation that consumes the pointers.

    The command must sit in the loop body — after its header and before its
    `done` — so a pytest run following the loop is not counted. Raises
    `AssertionError` when no command in that body invokes pytest with the
    positional expansion.
    """  # ruff: ignore[docstring-missing-returns, docstring-missing-exception] - the summary names the return and the refusal
    program = f"$({pytest_variable})"
    for start, words in command_segments(tokens):
        if not after < start < body_end or command_program(words) != program:
            continue
        require(
            condition="$$@" in words,
            message=(
                f"the `test-python` recipe's `{program}` command does not "
                "expand `$$@`, so the patterns the loop bound are discarded "
                "rather than passed to pytest"
            ),
        )
        return start
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
        to the positional parameters within the loop body, or does not pass
        those parameters to pytest there. Each message names the missing step.

    Examples
    --------
    >>> require_selector_drives_pytest(
    ...     "for p in $(foreach t,$(S),$(t)); do set -- $$p; $(PY) $$@; done",
    ...     selector="S",
    ...     pytest_variable="PY",
    ... )
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    tokens = recipe_tokens(recipe)
    loop, variable, body_end = _loop_header(tokens, selector=selector)
    # Locate pytest first: it is the fixed point the binding is read up to, so
    # a `set` after the pytest invocation cannot pass as the binding pytest
    # consumes, and a `set` between the two is seen as the overwrite it is.
    invocation = _pytest_command(
        tokens, pytest_variable=pytest_variable, after=loop, body_end=body_end
    )
    _positional_binding(tokens, variable=variable, after=loop, before=invocation)


def require_module_runs_under_pytest(
    recipe: str,
    *,
    module: str,
    pytest_variable: str,
) -> None:
    """Require a recipe to hand ``module`` to the configured pytest command.

    Used where a target is the *bootstrap* — deliberately outside the selector —
    and running one module by name is the whole point of it. A text check
    cannot hold that: the same characters in a commented-out entry, or in an
    `echo`'s arguments, would satisfy a substring test while the shell ran
    neither. The module must be an argument of a `pytest_variable` command.

    Parameters
    ----------
    recipe : str
        Recipe text, as `recipe_of` returns it.
    module : str
        The module path the recipe must run, relative to the working directory.
    pytest_variable : str
        The Makefile variable naming the pytest command.

    Raises
    ------
    AssertionError
        If no command the shell runs invokes pytest with ``module`` among its
        arguments.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    program = f"$({pytest_variable})"
    invoked = [
        words
        for _start, words in command_segments(recipe_tokens(recipe))
        if command_program(words) == program
    ]
    require(
        condition=bool(invoked),
        message=(
            f"the target's recipe must run pytest as `{program}`, naming "
            f"{module} among its arguments; no command the shell runs names it"
        ),
    )
    require(
        condition=any(module in words for words in invoked),
        message=(
            f"the target's recipe names {module} only outside a `{program}` "
            "command it runs — in dropped text, or among another command's "
            "arguments — so the module is described rather than collected"
        ),
    )


def require_selector_is_consumed(recipe: str, *, selector: str) -> None:
    """Require a recipe to hand the selector to a command it actually runs.

    Weaker than :func:`require_selector_drives_pytest`, and used where only the
    weaker claim is available: an exemption pairs a selector with a target,
    which may be any recipe in the Makefile, so the full loop shape cannot be
    required of it — only that the selector reaches a command the shell runs.

    The two ways of naming a selector without consuming it are refused
    separately. Text the shell drops — a commented-out entry — contributes no
    token at all, so tokenizing catches it where a search of the recipe string
    would not. Text a command merely *prints* is live, and describes the
    selector to a reader rather than passing it to anything.

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
        for _start, words in command_segments(recipe_tokens(recipe))
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
        condition=any(command_program(words) not in _PRINTERS for words in named),
        message=(
            f"the target's recipe names {reference} only among the arguments "
            "of a command that prints them, so the selector is described "
            "rather than consumed; nothing the target runs uses it"
        ),
    )
