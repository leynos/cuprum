"""Generated-input properties for the workflow's shell readers.

`tests/helpers/workflow_shell.py` reads `.github/workflows/ci.yml` so the
ratchet contract tests can assert on what a step's script *says*. Two of its
readers are small state machines over shell text: `top_level_operators` counts
an operator outside quotes and parentheses, and `shell_statements` splits a
body into commands and their guards. Every assertion those readers support
inherits their bugs, and the fixed examples beside them cannot show that the
state machines are right for the shapes they were not handed.

These properties generate those shapes. `top_level_operators` is checked
against an independent model that builds an expression together with the count
it must produce, so the expectation comes from the construction rather than
from a second reading of the scanner. The `shell_statements` properties are
metamorphic: they hold a relation between two bodies that differ only in a way
the reader is required to see through. A property that merely restated an
implementation would agree with it whatever it did.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.helpers.workflow_shell import shell_statements, top_level_operators

_WORD = st.text(alphabet="abc01", min_size=1, max_size=4)
_PLAIN = st.text(alphabet="abc01 ", max_size=6)


def _atom() -> st.SearchStrategy[tuple[str, int, bool]]:
    """Return a generator of a word, its top-level count, and its quotedness."""
    return _WORD.map(lambda word: (word, 0, False))


def _extend(
    children: st.SearchStrategy[tuple[str, int, bool]],
) -> st.SearchStrategy[tuple[str, int, bool]]:
    """Return a generator of one larger expression from *children*."""
    return st.one_of(
        children.flatmap(
            lambda left: children.map(
                lambda right: (
                    f"{left[0]} && {right[0]}",
                    left[1] + right[1] + 1,
                    left[2] or right[2],
                )
            )
        ),
        # Quoting is only inert when the quoted text carries no quote of its
        # own. `''0''` is two empty quoted strings around `0`, so the quotes
        # pair off and expose the operator between them; treating single
        # quotes as opaque nesting would model a language shell does not have.
        children.filter(lambda inner: not inner[2]).map(
            lambda inner: (f"'{inner[0]}'", 0, True)
        ),
        children.map(lambda inner: (f"({inner[0]})", 0, inner[2])),
    )


_EXPRESSIONS = st.recursive(_atom(), _extend, max_leaves=12)


@settings(max_examples=500)
@given(_EXPRESSIONS)
def test_top_level_operators_matches_its_construction(
    case: tuple[str, int, bool],
) -> None:
    """The count equals the number of operators the expression was built with.

    The expression and its expected count are generated together: the model
    adds one each time it joins two subexpressions with `&&`, and adds none
    when it wraps a subexpression in quotes or parentheses, which is exactly
    what binding at depth zero means. A scanner that counted a quoted or
    parenthesized operator would disagree with the model that built it.
    """
    expression, expected, _ = case

    assert top_level_operators(expression, "&&") == expected, (
        f"the scan of {expression!r} must count the operators its construction "
        f"left at the top level"
    )


@settings(max_examples=200)
@given(_EXPRESSIONS)
def test_top_level_operators_does_not_count_the_other_operator(
    case: tuple[str, int, bool],
) -> None:
    """An expression built only from `&&` carries no top-level `||`.

    A reader that matched on the shared `|` prefix, or that took the operator
    argument as advisory and counted any disjunction-like pair, would report a
    disjunction in a condition that is conjunctive throughout.
    """
    expression, _, _ = case

    assert top_level_operators(expression, "||") == 0, (
        f"{expression!r} conjunctive at every level must report no top-level "
        "disjunction"
    )


@settings(max_examples=200)
@given(left=_WORD, right=_WORD)
def test_a_semicolon_and_a_newline_separate_equally(left: str, right: str) -> None:
    """Both separators end a statement, so both yields the same pair.

    The guard reader exists because a workflow may write two commands on one
    line. If a newline separated and a semicolon did not, a reader would see a
    single statement ending in the second command's guard and report the first
    command as guarded when nothing guards it.
    """
    separated_by_semicolon = shell_statements(f"{left}; {right}")
    separated_by_newline = shell_statements(f"{left}\n{right}")

    assert separated_by_semicolon == separated_by_newline, (
        f"{left!r} and {right!r} must read as two statements however the "
        "workflow spells the separator"
    )
    assert len(separated_by_newline) == 2, (
        "the two commands must be seen as two, not merged into one"
    )


@settings(max_examples=200)
@given(_EXPRESSIONS)
def test_a_quoted_separator_does_not_split(case: tuple[str, int, bool]) -> None:
    """A separator inside quotes is part of the command, not a split point.

    Workflow scripts pass shell fragments as quoted arguments, so a reader
    that split on every separator would cut a command in half and then match
    neither half against the command a test is looking for.
    """
    expression, _, _ = case

    assert len(shell_statements(f"echo '{expression}'")) == 1, (
        f"a separator inside {expression!r} is quoted text, not a boundary"
    )


@settings(max_examples=200)
@given(_EXPRESSIONS)
def test_a_quoted_separator_does_not_split_the_body(
    case: tuple[str, int, bool],
) -> None:
    """A separator inside quotes does not split a body spanning a newline."""
    expression, _, _ = case

    assert len(shell_statements(f"echo '\n{expression}'")) == 1, (
        f"a separator inside {expression!r} is quoted text, not a boundary"
    )


@settings(max_examples=200)
@given(command=_WORD, comment=_PLAIN)
def test_a_comment_cannot_guard_a_command(command: str, comment: str) -> None:
    """Appending a commented-out guard does not change what a command reads as.

    A workflow's own prose quotes the guards these readers search for, and the
    contract tests assert that a command carries one. A reader that scanned the
    commented body would find the guard in the *comment* and report the command
    as guarded; a command cannot be guarded by text the shell never runs.
    """
    with_comment = shell_statements(f"{command} # {comment} || return $?")

    assert with_comment == shell_statements(command), (
        f"{command!r} must read the same with a commented-out guard appended as "
        "it does without one"
    )


@settings(max_examples=200)
@given(first=_WORD, second=_WORD)
def test_a_continuation_never_splits(first: str, second: str) -> None:
    """A backslash-continued command is one statement, not two.

    Continuations are layout, not command boundaries. A reader that let one
    split would detach a command from the guard on its continuation line and
    report the command as unguarded.
    """
    continued = shell_statements(f"{first} \\\n  {second}")

    assert len(continued) == 1, (
        f"{first!r} continued onto a second line must stay one statement"
    )
    assert first in continued[0], (
        f"the command before the continuation, {first!r}, must survive the join"
    )
    assert second in continued[0], (
        f"the continuation line's {second!r} must join the statement, not split it"
    )
