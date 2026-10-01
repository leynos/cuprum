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
consumers are `tests/helpers/suite_selection.py` and
`tests/test_ci_suite_wiring_contract.py`, which need both the resolved
selector and the recipes that consume it.

This module reads the Makefile; it does not decide what a selector *means*.
Resolving a pattern against the repository, and refusing a pattern that could
not be a `pytest` argument, are claims about the suite rather than about
`make`, and they live in `tests/helpers/suite_selection.py` beside the rest of
the selection policy.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv
import typing as typ

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

__all__ = (
    "MAKEFILE",
    "MAKEUTIL_TIMEOUT_SECONDS",
    "Runner",
    "makeutil_document",
    "recipe_of",
    "recipe_tokens",
    "variable_expansion",
)

#: The Makefile this module reads. One definition, so a test that needs to
#: name it and a helper that needs to read it cannot disagree.
MAKEFILE = "Makefile"

#: How long the parser may take before the read is abandoned. `makeutil` parses
#: one file in well under a second, so this only fires on a wedged process; the
#: bound exists to turn "the suite hangs" into a named contract failure.
MAKEUTIL_TIMEOUT_SECONDS: typ.Final = 60

#: The process boundary, as a type rather than as an import. The parser is the
#: one thing here that reads the outside world, so it is the one thing worth
#: substituting: a test that has to install `makeutil` to exercise a malformed
#: document is testing the toolchain, and cannot exercise a missing binary or a
#: timeout at all. The parameter list is `subprocess.run`'s, narrowed to the
#: keywords `_parse_with` passes.
type Runner = cabc.Callable[..., subprocess.CompletedProcess[str]]

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


def _parse_with(
    runner: Runner,
    *,
    makefile: str,
    root: pth.Path,
) -> subprocess.CompletedProcess[str]:
    """Run the parser, reporting a process that never started as a contract error.

    `makeutil` is installed by CI and by `make`, so an environment without it
    is a genuine failure the caller must see. `subprocess.run` reports it as
    `FileNotFoundError`, which is a *different* type from the `AssertionError`
    the read API documents — so a caller catching the documented error would
    miss it, and one catching everything would not know which tool was absent.
    Translating here keeps the module's contract honest: every way the read can
    fail arrives as the documented failure, naming the binary and the directory
    it was looked for in.

    A timeout is translated for the same reason, and matters more: an
    unhandled `TimeoutExpired` would escape as a traceback from a library the
    caller never invoked.

    Parameters
    ----------
    runner : Runner
        The process boundary, as :func:`makeutil_document` received it.
    makefile : str
        Path to the Makefile, relative to the working directory.
    root : pathlib.Path
        The working directory the parser is run in.

    Returns
    -------
    subprocess.CompletedProcess
        The completed process, whatever its exit status; a non-zero status is
        the caller's to report, because it carries the parser's own diagnostic.

    Raises
    ------
    AssertionError
        If the binary cannot be started, or does not finish within
        :data:`MAKEUTIL_TIMEOUT_SECONDS`.
    """
    try:
        return runner(
            ["makeutil", "parse", makefile],
            capture_output=True,
            text=True,
            cwd=root,
            check=False,
            timeout=MAKEUTIL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        message = (
            f"makeutil is not on PATH, so {makefile} could not be parsed; the "
            "selector is unreadable rather than empty. Install it the way CI "
            "does (`make` does this as a prerequisite), or pass a `runner` "
            "that supplies a parsed document"
        )
        raise AssertionError(message) from error
    except subprocess.TimeoutExpired as error:
        message = (
            f"makeutil did not parse {makefile} within "
            f"{MAKEUTIL_TIMEOUT_SECONDS}s; the process was still running, so "
            "this is a wedged parser rather than a malformed Makefile"
        )
        raise AssertionError(message) from error


def makeutil_document(
    *,
    makefile: str = MAKEFILE,
    root: pth.Path | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, typ.Any]:
    """Parse one Makefile with the pinned `makeutil` binary.

    Parameters
    ----------
    makefile : str
        Path to the Makefile, relative to the working directory.
    root : pathlib.Path, optional
        The directory to parse in, defaulting to the repository root. Named
        rather than assumed so a caller reading a different tree does not have
        its path silently resolved against this one.
    runner : Runner, optional
        The process boundary, defaulting to `subprocess.run`. Injected so the
        parser's own behaviour — a non-zero exit, malformed JSON, a missing
        binary, a timeout — is exercised without installing `makeutil`, which
        is what makes those cases testable at all.

    Returns
    -------
    dict
        The parsed JSON document, with `variables`, `rules`, and `includes`.

    Raises
    ------
    AssertionError
        If `makeutil` cannot be started, does not finish within
        :data:`MAKEUTIL_TIMEOUT_SECONDS`, exits non-zero, or emits something
        other than a JSON object. Each means the parse did not happen, and a
        caller that read the empty result as "the selector names nothing"
        would fail later with a misleading message.
    """
    completed = _parse_with(runner, makefile=makefile, root=root or repo_root())
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


def variable_expansion(
    name: str,
    *,
    makefile: str = MAKEFILE,
    root: pth.Path | None = None,
    runner: Runner = subprocess.run,
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
    """  # ruff: ignore[docstring-extraneous-exception] - contract errors propagate from _require()
    records = _variable_records(
        makeutil_document(makefile=makefile, root=root, runner=runner)
    )
    _require(
        condition=name in records,
        message=f"the Makefile must assign {name}",
    )
    return tuple(_expand(records[name], records).split())


def recipe_of(
    name: str,
    *,
    makefile: str = MAKEFILE,
    root: pth.Path | None = None,
    runner: Runner = subprocess.run,
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
    >>> recipe_tokens("@echo hi # $(PYTEST)")
    ('echo', 'hi')
    >>> recipe_tokens("echo '# $(PYTEST)'")
    ('echo', '# $(PYTEST)')
    """
    lexer = shlex.shlex(recipe, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return tuple(lexer)
