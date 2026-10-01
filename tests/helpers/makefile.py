"""Read what this repository's Makefile says, the way the selector reads it.

The selector lives in `PYTEST_TARGETS` as a list of shell glob patterns, and
the pattern that decides whether a contract module is collected is the one that
names it. Reading that variable by hand, or by a regex over the source,
under-reports it in ways that look like a clean result: a missed continuation,
a comment mistaken for an assignment, or a `$(VAR)` reference returned as its
own literal text all shrink the set, and a set that is too small makes every
"nothing is uncovered" assertion pass for the wrong reason.

So the parse is not hand-rolled; it is not here either. Getting the parsed
document — reaching for `makeutil`, running it, and reporting a process that
never started — is `tests/helpers/makeutil.py`, whose `makeutil_document`
returns the `variables`, `rules`, and `includes` this module reads. The split
follows `workflow_shell` and `workflow_recipe`: reaching for a thing the caller
named, versus deriving from a thing it already holds.

What lives here is `make`'s own semantics for the document that parse returns:
which assignment wins, how continuations collapse, how `$(VAR)` references
resolve, and what a target's recipe says. Its consumers are
`tests/helpers/suite_selection.py` and `tests/test_ci_suite_wiring_contract.py`,
which need both the resolved selector and the recipes that consume it.

This module reads the Makefile; it does not decide what a selector *means*.
Resolving a pattern against the repository, and refusing a pattern that could
not be a `pytest` argument, are claims about the suite rather than about
`make`, and they live in `tests/helpers/suite_selection.py` beside the rest of
the selection policy.
"""

from __future__ import annotations

import re
import shlex
import typing as typ

from tests.helpers.ci_documents import require
from tests.helpers.makeutil import (
    DEFAULT_RUNNER,
    MAKEFILE,
    Runner,
    makeutil_document,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

# `MAKEFILE`, `Runner`, and `makeutil_document` are re-exported rather than
# defined here: a caller reading a variable or a recipe names one module, and
# the two halves of that read are not a distinction it should have to hold.
# `makeutil.py` remains their definition, so a change to the process boundary
# has exactly one place to land.
__all__ = (
    "MAKEFILE",
    "Runner",
    "makeutil_document",
    "recipe_of",
    "recipe_tokens",
    "variable_expansion",
)

#: Operators that always take effect, so the last of them wins. The empty
#: string is a recipe-line assignment: `makeutil` reports the bodies of
#: `define` blocks and of recipes under the name they are attached to.
_PLAIN_OPERATORS = frozenset({"", "=", ":=", "::="})

#: The conditional operator, which assigns only if the variable is not already
#: defined by that point in the file. A `?=` below an earlier assignment to the
#: same name therefore changes nothing.
_CONDITIONAL_OPERATOR = "?="


def _assignments(document: dict[str, typ.Any]) -> cabc.Iterator[tuple[str, str, str]]:
    """Yield each usable ``(name, raw_value, operator)`` triple in file order.

    `makeutil`'s JSON is foreign data, so a record missing a field, or carrying
    one that is not a string, is skipped rather than trusted. Filtering here
    keeps the narrowing out of the caller, which then holds only `make`'s own
    assignment rules.

    Parameters
    ----------
    document : dict
        The `makeutil` JSON document.

    Yields
    ------
    tuple of str
        The name, raw value, and operator of one assignment.
    """
    declared = typ.cast("list[object]", document.get("variables") or [])
    for record in declared:
        entry = typ.cast("dict[str, object]", record)
        fields = tuple(entry.get(field) for field in ("name", "raw_value", "operator"))
        if all(isinstance(field, str) for field in fields):
            yield typ.cast("tuple[str, str, str]", fields)


def _variable_records(document: dict[str, typ.Any]) -> dict[str, str]:
    """Return every assignment's ``name`` to ``raw_value`` mapping.

    Each assignment is applied in file order under `make`'s own rules, because
    the operator decides whether an assignment takes effect. Only ``=``,
    ``:=``, and ``::=`` replace an earlier definition unconditionally; ``?=``
    only assigns where the variable is still undefined. Applying every
    assignment in order would resolve `PYTEST_TARGETS` to a later ``?=`` that
    `make` ignores, so the guard would police a selector `make test-python`
    never consumes.

    Parameters
    ----------
    document : dict
        The `makeutil` JSON document.

    Returns
    -------
    dict of str to str
        The value `make` ends up with for each assigned name.

    Raises
    ------
    AssertionError
        If the document carries no `variables` list, so an unexpected parser
        change fails here rather than silently reading nothing, or if an
        assignment uses an operator this module does not implement. An
        unimplemented operator cannot be folded in as though it were `=`
        without risking the same wrong value.

    Notes
    -----
    This reads the file, not an invocation: a variable passed on `make`'s own
    command line is defined before the Makefile is read, so a ``?=`` for it
    does nothing, and a caller relying on such an override would see the
    file's value here. CI invokes no overrides, so that gap is out of scope
    rather than unconsidered.

    ``:=`` and ``::=`` are stored as raw text like ``=``, so their value is
    expanded late rather than at their own line. That differs from `make` only
    where the right-hand side references a name assigned again later, which
    this Makefile does exactly once, in ``MATURIN_DEVELOP_IS_RELEASE``. No
    selector's closure reaches it, and resolving it would mean evaluating
    `make`'s ``strip``, ``filter``, and ``call``; ``_expand`` reports such a
    function rather than returning a plausible wrong answer.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    declared = document.get("variables")
    require(
        condition=isinstance(declared, list),
        message="the makeutil document must carry a `variables` list",
    )
    records: dict[str, str] = {}
    for name, raw_value, operator in _assignments(document):
        if operator == _CONDITIONAL_OPERATOR:
            records.setdefault(name, raw_value)
        else:
            require(
                condition=operator in _PLAIN_OPERATORS,
                message=(
                    f"the Makefile assigns {name} with {operator!r}, which this "
                    "reader does not implement; add it to _PLAIN_OPERATORS or "
                    f"handle it as {_CONDITIONAL_OPERATOR} is handled"
                ),
            )
            records[name] = raw_value
    require(
        condition=bool(records),
        message="the makeutil document must report at least one assignment",
    )
    return records


def _join_continuations(value: str) -> str:
    r"""Collapse ``\\``-newline continuations the way `make` does.

    Parameters
    ----------
    value : str
        An assignment's raw text, possibly spanning several source lines.

    Returns
    -------
    str
        The logical single line: `make` replaces each backslash-newline and
        the whitespace after it with one space, so a two-line list falls apart
        into the same words a one-line list does. Leaving them in place would
        make each backslash a word in its own right, and it is not a `.py`
        path — the pattern beside it would still resolve, so the selector would
        look right while the tuple carried junk.
    """
    return re.sub(r"\\\n[ \t]*", " ", value)


def _expand(
    value: str,
    records: cabc.Mapping[str, str],
    *,
    seen: frozenset[str] = frozenset(),
) -> str:
    """Expand every ``$(VAR)`` reference in ``value``, recursively.

    Parameters
    ----------
    value : str
        Text carrying zero or more ``$(VAR)`` references.
    records : Mapping of str to str
        The assignment table to resolve against.
    seen : frozenset of str
        Names already being expanded on this path, so a cycle is reported
        rather than recursed into forever.

    Returns
    -------
    str
        ``value`` with every reference replaced by its resolved expansion.

    Raises
    ------
    AssertionError
        If a reference names a variable the Makefile does not assign, or if a
        reference cycle is found. Neither is recoverable: substituting an
        empty string would shrink the selector and turn the guard vacuous.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    value = _join_continuations(value)
    resolved: list[str] = []
    index = 0
    while index < len(value):
        opener = value.find("$(", index)
        if opener == -1:
            resolved.append(value[index:])
            break
        closer = value.find(")", opener)
        require(
            condition=closer != -1,
            message=f"unbalanced `$(` in {value!r}",
        )
        resolved.append(value[index:opener])
        name = value[opener + 2 : closer]
        require(
            condition=name in records,
            message=(
                f"the Makefile expands $({name}) but never assigns it; the "
                "selector cannot be resolved"
            ),
        )
        require(
            condition=name not in seen,
            message=f"$({name}) expands itself: {sorted(seen)}",
        )
        resolved.append(_expand(records[name], records, seen=seen | {name}))
        index = closer + 1
    return "".join(resolved)


def variable_expansion(
    name: str,
    *,
    makefile: str = MAKEFILE,
    root: pth.Path | None = None,
    runner: Runner = DEFAULT_RUNNER,
) -> tuple[str, ...]:
    """Expand a Makefile variable into the whitespace-separated words it names.

    The parser's `raw_value` keeps each assignment's backslash-newlines
    verbatim, so the continuations are collapsed by `_expand` — via
    `_join_continuations` — before the split. Without that step every
    continuation backslash would become a word of its own and the selector
    would carry junk alongside the patterns it names.

    Parameters
    ----------
    name : str
        Variable name, without the assignment operator.
    makefile : str
        Path to the Makefile, relative to the working directory.
    root : pathlib.Path, optional
        The directory to parse in, as :func:`makeutil_document` takes it.
    runner : Runner, optional
        The process boundary, as :func:`makeutil_document` takes it, so a test
        can drive this question without installing `makeutil`.

    Returns
    -------
    tuple of str
        The expanded words, in declaration order. An assignment that expands
        to nothing returns an empty tuple.

    Raises
    ------
    AssertionError
        If the variable is not assigned, if it references an undefined
        variable, or if it references itself; or if the parse itself fails, as
        :func:`makeutil_document` reports.
    """  # ruff: ignore[docstring-extraneous-exception] - contract errors propagate from require()
    records = _variable_records(
        makeutil_document(makefile=makefile, root=root, runner=runner)
    )
    require(
        condition=name in records,
        message=f"the Makefile must assign {name}",
    )
    return tuple(_expand(records[name], records).split())


def recipe_of(
    name: str,
    *,
    makefile: str = MAKEFILE,
    root: pth.Path | None = None,
    runner: Runner = DEFAULT_RUNNER,
) -> str:
    """Return one target's recipe text.

    Parameters
    ----------
    name : str
        Target name, as written before the colon.
    makefile : str
        Path to the Makefile, relative to the working directory.
    root : pathlib.Path, optional
        The directory to parse in, as :func:`makeutil_document` takes it.
    runner : Runner, optional
        The process boundary, as :func:`makeutil_document` takes it, so a test
        can drive this question without installing `makeutil`.

    Returns
    -------
    str
        The target's recipe lines joined with newlines, with `make`'s leading
        `@` silencing marker removed and backslash continuations collapsed.
        Line structure is otherwise preserved, so a caller can still tell one
        command from the next.

    Raises
    ------
    AssertionError
        If the Makefile declares no rule for ``name``, or if the parse itself
        fails, as :func:`makeutil_document` reports.
    """
    document = makeutil_document(makefile=makefile, root=root, runner=runner)
    declared = document.get("rules")
    require(
        condition=isinstance(declared, list),
        message="the makeutil document must carry a `rules` list",
    )
    for rule in typ.cast("list[object]", declared):
        entry = typ.cast("dict[str, object]", rule)
        targets = typ.cast("list[object]", entry.get("targets") or [])
        if name not in targets:
            continue
        recipes = typ.cast("list[object]", entry.get("recipes") or [])
        return "\n".join(
            _join_continuations(
                typ.cast("str", typ.cast("dict[str, object]", step).get("text", ""))
            ).removeprefix("@")
            for step in recipes
        )
    require(condition=False, message=f"the Makefile must declare a {name} target")
    raise AssertionError


def recipe_tokens(recipe: str) -> tuple[str, ...]:
    """Tokenize a recipe into the shell words `make` would hand the shell.

    A caller asking whether a recipe *uses* a construct has to read it as a
    shell rather than as text. A substring test cannot tell a live command from
    the same words commented out, and this repository's recipes are joined by
    `recipe_of` into one long line, so a single stray ``#`` would silently
    disable everything after it while every token check kept passing.

    Comment markers are honoured, which is the whole point: ``# ...``
    contributes no tokens, so dead text cannot satisfy a caller. Quoting is
    honoured too, so a literal inside a quoted argument counts as a word rather
    than as a comment opening.

    Parameters
    ----------
    recipe : str
        Recipe text, as :func:`recipe_of` returns it.

    Returns
    -------
    tuple of str
        The shell words, in order, with comments dropped.

    Examples
    --------
    >>> recipe_tokens("echo hi # $(PYTEST)")
    ('echo', 'hi')
    >>> recipe_tokens("echo '# $(PYTEST)'")
    ('echo', '# $(PYTEST)')
    """
    lexer = shlex.shlex(recipe, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return tuple(lexer)
