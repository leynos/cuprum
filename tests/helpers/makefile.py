"""Read this repository's Makefile the way the suite selector reads it.

The selector lives in `PYTEST_TARGETS` as a list of shell glob patterns, and
the pattern that decides whether a contract module is collected is the one
that names it. Reading that variable by hand, or by a regex over the source,
under-reports it in ways that look like a clean result: a missed continuation,
a comment mistaken for an assignment, or a `$(VAR)` reference returned as its
own literal text all shrink the set, and a set that is too small makes every
"nothing is uncovered" assertion pass for the wrong reason.

So the parse is not hand-rolled. `makeutil`, the pinned parser the repository
already depends on, reports each assignment's `raw_value` with its
continuations and each rule's recipe text, and this module reads that. Its
first consumer is `tests/test_ci_test_selection_contract.py`, which needs both
the resolved selector and the recipes that consume it; the guard's docstring
records why the two are read together.
"""

from __future__ import annotations

import json
import re
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv
import typing as typ

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

__all__ = (
    "MAKEFILE",
    "makeutil_document",
    "recipe_of",
    "selected_paths",
    "variable_expansion",
)

#: The Makefile this module reads. One definition, so a test that needs to
#: name it and a helper that needs to read it cannot disagree.
MAKEFILE = "Makefile"

#: Marker distinguishing "this pattern names files" from "this pattern is a
#: bare word". A `pytest` argument with no `.py` in it is not a path.
_PATH_SUFFIX = ".py"

#: Operators that always take effect, so the last of them wins. The empty
#: string is a recipe-line assignment: `makeutil` reports the bodies of
#: `define` blocks and of recipes under the name they are attached to.
_PLAIN_OPERATORS = frozenset({"", "=", ":=", "::="})

#: The conditional operator, which assigns only if the variable is not already
#: defined by that point in the file. A `?=` below an earlier assignment to the
#: same name therefore changes nothing.
_CONDITIONAL_OPERATOR = "?="


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


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
    This reads the file, not an invocation. A variable passed on `make`'s own
    command line, or present in the environment, is defined before the
    Makefile is read, so a ``?=`` for it does nothing — and a caller that
    relied on such an override would see the file's value here. The guard
    asks what the repository declares and CI invokes no overrides, so that
    gap is out of scope rather than unconsidered.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from _require()
    declared = document.get("variables")
    _require(
        condition=isinstance(declared, list),
        message="the makeutil document must carry a `variables` list",
    )
    records: dict[str, str] = {}
    for name, raw_value, operator in _assignments(document):
        if operator == _CONDITIONAL_OPERATOR:
            records.setdefault(name, raw_value)
        else:
            _require(
                condition=operator in _PLAIN_OPERATORS,
                message=(
                    f"the Makefile assigns {name} with {operator!r}, which this "
                    "reader does not implement; add it to _PLAIN_OPERATORS or "
                    f"handle it as {_CONDITIONAL_OPERATOR} is handled"
                ),
            )
            records[name] = raw_value
    _require(
        condition=bool(records),
        message="the makeutil document must report at least one assignment",
    )
    return records


def makeutil_document(*, makefile: str = MAKEFILE) -> dict[str, typ.Any]:
    """Parse one Makefile with the pinned `makeutil` binary.

    Parameters
    ----------
    makefile : str
        Path to the Makefile, relative to the repository root.

    Returns
    -------
    dict
        The parsed JSON document, with `variables`, `rules`, and `includes`.

    Raises
    ------
    AssertionError
        If `makeutil` exits non-zero or emits something other than a JSON
        object. Both mean the parse did not happen, and a caller that treated
        the empty result as "the selector names nothing" would fail a moment
        later with a misleading message.
    """
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argument vector.
        ["makeutil", "parse", makefile],  # ruff: ignore[start-process-with-partial-path] - `makeutil` resolved from PATH.
        capture_output=True,
        text=True,
        cwd=repo_root(),
        check=False,
    )
    _require(
        condition=completed.returncode == 0,
        message=(
            f"makeutil failed to parse {makefile} with exit "
            f"{completed.returncode}: {completed.stderr.strip()}"
        ),
    )
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        message = f"makeutil did not emit JSON for {makefile}: {error}"
        raise AssertionError(message) from error
    _require(
        condition=isinstance(parsed, dict),
        message=f"makeutil must emit a JSON object for {makefile}",
    )
    return typ.cast("dict[str, typ.Any]", parsed)


def _join_continuations(value: str) -> str:
    r"""Collapse ``\\``-newline continuations the way `make` does.

    Parameters
    ----------
    value : str
        An assignment's raw text, possibly spanning several source lines.

    Returns
    -------
    str
        The logical single line. `make` replaces each backslash-newline and the
        whitespace that follows it with one space, so a two-line list fails
        apart into the same words a one-line list does. Leaving the backslashes
        in place would make each of them a word in its own right, and a
        backslash is not a `.py` path — the pattern beside it would still
        resolve, so the selector would look right while the tuple carried junk.
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
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from _require()
    value = _join_continuations(value)
    resolved: list[str] = []
    index = 0
    while index < len(value):
        opener = value.find("$(", index)
        if opener == -1:
            resolved.append(value[index:])
            break
        closer = value.find(")", opener)
        _require(
            condition=closer != -1,
            message=f"unbalanced `$(` in {value!r}",
        )
        resolved.append(value[index:opener])
        name = value[opener + 2 : closer]
        _require(
            condition=name in records,
            message=(
                f"the Makefile expands $({name}) but never assigns it; the "
                "selector cannot be resolved"
            ),
        )
        _require(
            condition=name not in seen,
            message=f"$({name}) expands itself: {sorted(seen)}",
        )
        resolved.append(_expand(records[name], records, seen=seen | {name}))
        index = closer + 1
    return "".join(resolved)


def variable_expansion(name: str, *, makefile: str = MAKEFILE) -> tuple[str, ...]:
    """Expand a Makefile variable into the whitespace-separated words it names.

    Continuation lines are already collapsed by the parser's `raw_value`, so
    the split is over the logical assignment rather than its source layout.

    Parameters
    ----------
    name : str
        Variable name, without the assignment operator.
    makefile : str
        Path to the Makefile, relative to the repository root.

    Returns
    -------
    tuple of str
        The expanded words, in declaration order. An assignment that expands
        to nothing returns an empty tuple.

    Raises
    ------
    AssertionError
        If the variable is not assigned, if it references an undefined
        variable, or if it references itself.
    """  # ruff: ignore[docstring-extraneous-exception] - contract errors propagate from _require()
    records = _variable_records(makeutil_document(makefile=makefile))
    _require(
        condition=name in records,
        message=f"the Makefile must assign {name}",
    )
    return tuple(_expand(records[name], records).split())


def selected_paths(
    patterns: cabc.Iterable[str], *, root: pth.Path | None = None
) -> tuple[pth.Path, ...]:
    """Resolve selector patterns against the repository root.

    Parameters
    ----------
    patterns : Iterable of str
        Shell glob patterns, as a selector variable expands to them.
    root : Path or None
        Directory to resolve against. Defaults to the repository root.

    Returns
    -------
    tuple of Path
        Every file the patterns name, relative to ``root``, sorted and
        deduplicated. A pattern matching nothing contributes nothing, which is
        how `make test-python` itself behaves: its loop skips a pattern whose
        first expansion does not exist.

    Notes
    -----
    A pattern without a `.py` suffix is treated as naming no files rather than
    as a file named literally. The selector is a list of Python test paths and
    the recipes pass it straight to `pytest`, so a bare word in it is a
    mistake, not a file. Callers that need to police that case should assert
    the word appears in the variable rather than expecting a path here.
    """
    base = repo_root() if root is None else root
    found: set[pth.Path] = set()
    for pattern in patterns:
        if not pattern.endswith(_PATH_SUFFIX):
            continue
        found.update(
            path.relative_to(base) for path in base.glob(pattern) if path.is_file()
        )
    return tuple(sorted(found))


def recipe_of(name: str, *, makefile: str = MAKEFILE) -> str:
    """Return one target's recipe text.

    Parameters
    ----------
    name : str
        Target name, as written before the colon.
    makefile : str
        Path to the Makefile, relative to the repository root.

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
        If the Makefile declares no rule for ``name``.
    """
    document = makeutil_document(makefile=makefile)
    declared = document.get("rules")
    _require(
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
    _require(condition=False, message=f"the Makefile must declare a {name} target")
    raise AssertionError
