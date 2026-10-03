"""Property tests for the readers that decide what a Makefile assignment means.

`makefile._expand` and `recipe_read._join_continuations` resolve text no
reviewer reads line by line: an assignment spread over continuation lines, the
references inside it, and the recipe entries that follow. `_expand` is the only
thing between the selector variable and the modules the guard then claims are
covered, and `recipe_tokens` is the only thing between a recipe and the claim
that it *uses* a construct rather than mentioning one in a comment.

The example tests beside these pin the shapes this repository declares today,
so every case they can express is one the estate already satisfies; a reader
could stop reading and the contracts above it would keep passing. The input
space is generated here instead of enumerated, and every property is checked
against a small independent model rather than a second copy of the reader, so
the two agreeing means both are right about the invariant rather than both
sharing a defect.

Each generator guarantees the shape its property is about, and each property
asserts that shape before asserting the behaviour, so a generator that stopped
producing the interesting case fails its own witness rather than leaving the
property true and empty.
"""

from __future__ import annotations

import string
import typing as typ

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.helpers import makefile, recipe_read
from tests.helpers.recipe_read import recipe_tokens

if typ.TYPE_CHECKING:
    from hypothesis.strategies import DrawFn

#: Hypothesis's default deadline measures the host rather than the code, and a
#: flaky contract test is worse than a slow one. The same setting and the same
#: reason as `tests/test_ci_placement_properties.py`.
SETTINGS = settings(deadline=None, max_examples=200)

#: A word that tokenizes to itself: no whitespace, quoting, comment marker, or
#: backslash, and a `$`-free alphabet so it cannot read as a reference.
WORD = st.text(alphabet=string.ascii_lowercase + "-._/", min_size=1, max_size=8)


@st.composite
def _assignment_pieces(draw: DrawFn, remaining: list[str]) -> list[str]:
    """Draw one assignment's pieces, ordered so a continuation is guaranteed.

    Only names in ``remaining`` may be referenced, which is what makes the
    graph acyclic by construction rather than by rejection, and at least one
    reference and one literal are drawn whenever a later name exists, which
    makes the substitution and the two-line continuation structural rather than
    something a draw might omit.

    Returns
    -------
    list of str
        The ordered pieces: ``$(Vn)`` references and ``.py`` literals.
    """
    references = draw(
        st.lists(st.sampled_from(remaining), min_size=1, max_size=2)
        if remaining
        else st.just([])
    )
    literals = draw(
        st.lists(WORD.map(lambda word: f"{word}.py"), min_size=1, max_size=2)
    )
    return draw(st.permutations([f"$({ref})" for ref in references] + literals))


@st.composite
def _acyclic_graph(
    draw: DrawFn,
) -> tuple[str, list[dict[str, str]], tuple[str, ...]]:
    """Build an acyclic variable graph, its records, and the words it derives.

    Names are ``V0..Vn`` and each assignment may reference only *later* names,
    so no reference can close a cycle: a cycle is a refusal with its own
    example test, and generating one here would exercise that branch instead of
    the expansion being asserted. Assignments are built from the last name
    backwards, which is what lets each reference's words be resolved as it is
    drawn. Every literal is a ``.py`` word, so a stray backslash or an
    unsubstituted reference shows up as a word that is not one.

    The shapes the properties depend on are guaranteed rather than hoped for.
    Two names minimum means at least one assignment has a later name to
    reference, and each such assignment draws at least one reference and at
    least one literal, so the graph always carries a reference and always spans
    more than one source line. An earlier draft drew all three from zero and
    Hypothesis promptly shrank to the empty graph, where every assertion was
    true and nothing was covered.

    Returns
    -------
    tuple of (str, list of dict, tuple of str)
        The name to expand, the ``makeutil``-shaped assignment records for the
        whole graph, and the *complete* word sequence that name derives, in
        order and with duplicates — the model the expansion is compared to. It
        is returned because deriving it is the only reason the graph is built
        backwards, and a property that cannot see it can only assert the
        expansion is non-empty.
    """
    count = draw(st.integers(min_value=2, max_value=4))
    names = [f"V{index}" for index in range(count)]
    records: list[dict[str, str]] = []
    derived: dict[str, list[str]] = {}
    for index in range(count - 1, -1, -1):
        name = names[index]
        pieces = draw(_assignment_pieces(names[index + 1 :]))
        # Indent every source line but the first, so an assignment's
        # continuation is a real one rather than a one-line list.
        raw = " \\\n  ".join(pieces)
        records.append({"name": name, "raw_value": raw, "operator": "="})
        derived[name] = [
            word
            for piece in pieces
            for word in (derived[piece[2:-1]] if piece.startswith("$(") else [piece])
        ]
    return names[0], records, tuple(derived[names[0]])


@SETTINGS
@given(graph=_acyclic_graph())
def test_expansion_substitutes_every_reference_and_collapses_continuations(
    graph: tuple[str, list[dict[str, str]], tuple[str, ...]],
) -> None:
    """Expand an acyclic graph into exactly the words its pieces derived.

    The model concatenates each reference's own words in place, which is what
    `make` does. A reader that dropped a reference would shrink the selector,
    and a selector that is too small makes every coverage claim above it pass
    for the wrong reason; a reader that left a backslash in place would put a
    word in the tuple that is not a path pattern.

    The expansion is compared to the derived sequence as a whole, not merely
    inspected for bad words: order and duplicates are part of what the selector
    *means*, and a reader that sorted its output or deduplicated it would pass
    every check above while collecting a different set of modules.
    """
    name, records, expected = graph
    table = makefile._variable_records({"variables": records})
    expanded = makefile._expand(table[name], table)
    words = tuple(expanded.split())
    assert words == expected, (
        f"{name} must expand to exactly the words its pieces derived, in "
        f"order and with duplicates; got {words!r} against {expected!r}"
    )
    assert expected, (
        f"every generated assignment carries at least one literal, so {name} "
        "must derive a word; the model is empty, so this control is inert"
    )
    assert all(word.endswith(".py") for word in words), (
        f"every generated piece is a `.py` word or a reference to one, so "
        f"{words!r} names something that was neither substituted nor collapsed"
    )
    assert "$(" not in expanded, (
        f"expansion left a reference behind in {expanded!r}; a reference that "
        "is not substituted shrinks the selector silently"
    )
    assert "\\" not in expanded, (
        f"expansion left a continuation backslash in {expanded!r}; the word it "
        "would stand beside is not a path pattern"
    )


@SETTINGS
@given(graph=_acyclic_graph())
def test_every_name_in_an_acyclic_graph_resolves(
    graph: tuple[str, list[dict[str, str]], tuple[str, ...]],
) -> None:
    """Every intermediate name expands, not just the one asked for first."""
    _name, records, _expected = graph
    table = makefile._variable_records({"variables": records})
    for record in records:
        expanded = makefile._expand(table[record["name"]], table)
        assert "$(" not in expanded, (
            f"{record['name']} still carries a reference after expansion: "
            f"{expanded!r}; resolving a nested reference is what this reader "
            "is for, and a dropped one shrinks the selector silently"
        )


@SETTINGS
@given(lines=st.lists(WORD, min_size=1, max_size=5), pad=st.integers(0, 2))
def test_a_continuation_collapses_to_one_space(lines: list[str], pad: int) -> None:
    """Join a continued value into one line, keeping only what make keeps.

    `make` replaces the backslash, the newline, and the whitespace after it with
    one space. A space *before* the backslash is none of those things and
    survives, so an indented continuation joins as two spaces — which is why
    `pad` is drawn rather than assumed away: the reader's own docstring calls
    that out, and a model predicting a single space would be wrong about it.
    What must never survive is the backslash itself, which would become a word
    in its own right, or the newline, which would split one assignment into two
    the file never wrote.
    """
    continued = (" " * pad + "\\\n  ").join(lines)
    joined = recipe_read._join_continuations(continued)
    expected = (" " * (pad + 1)).join(lines)
    assert joined == expected, (
        f"{continued!r} must join to {expected!r}; the reader produced {joined!r}"
    )
    assert "\\" not in joined, (
        f"a joined value must not carry a backslash; {joined!r} does, and it "
        "would become a word of its own"
    )
    assert "\n" not in joined, (
        f"a joined value must not carry a newline; {joined!r} does, and it "
        "would split one assignment into two the file never wrote"
    )


@SETTINGS
@given(parts=st.lists(WORD, min_size=1, max_size=6))
def test_tokens_are_the_words_a_plain_split_would_report(parts: list[str]) -> None:
    """Quoted and unquoted words both survive, and nothing else appears.

    No generated word carries a backslash, so `recipe_tokens` must report
    exactly the words written. A tokenizer that dropped a quoted word, or
    invented one from the quoting, would change how many arguments a recipe's
    command receives.
    """
    quoted = [f"'{part}'" if index % 2 else part for index, part in enumerate(parts)]
    recipe = " ".join(quoted)
    tokens = recipe_tokens(recipe)
    assert tokens == tuple(parts), (
        f"{recipe!r} must tokenize to {tuple(parts)!r}; the reader reported "
        f"{tokens!r}, so quoting or whitespace changed the word list"
    )


@SETTINGS
@given(
    live=st.lists(WORD, min_size=1, max_size=4),
    dead=st.lists(WORD, min_size=1, max_size=4),
    marker=st.sampled_from(["$(PYTEST)", "$(SELECTOR)", "foreach"]),
)
def test_a_comment_contributes_no_token(
    live: list[str], dead: list[str], marker: str
) -> None:
    """Dead text must not satisfy a caller; the marker is the interesting word.

    Every token check above this reads the token list rather than the recipe
    text precisely so a commented-out command cannot stand in for a live one.
    The marker appears only inside the comment, so a reader that ignored
    commenters would report it and this would fail.
    """
    recipe = " ".join(live) + " # " + " ".join([*dead, marker])
    tokens = recipe_tokens(recipe)
    assert tokens == tuple(live), (
        f"{recipe!r} must tokenize to the live words {tuple(live)!r}; the "
        f"reader reported {tokens!r}"
    )
    assert marker not in tokens, (
        f"the commented marker {marker!r} is dead text and must not satisfy a "
        "token check reading this recipe"
    )


@SETTINGS
@given(graph=_acyclic_graph())
def test_the_graph_generator_produces_a_reference_and_a_continuation(
    graph: tuple[str, list[dict[str, str]], tuple[str, ...]],
) -> None:
    """Witness the two shapes the graph properties would otherwise miss.

    A generator that silently stopped drawing references, or stopped spanning
    two source lines, would leave every assertion above true and empty. This
    fails on that regression instead, which is the same role the anti-vacuity
    assertions inside the properties play for the clauses.
    """
    _name, records, expected = graph
    assert len(expected) > 1, (
        "the model must derive more than one word for the comparison above to "
        f"be about substitution rather than pass-through; got {expected!r}"
    )
    assert any("$(" in record["raw_value"] for record in records), (
        "the graph generator must produce at least one reference; without one "
        "the expansion properties only exercise literal pass-through"
    )
    assert any("\\\n  " in record["raw_value"] for record in records), (
        "the graph generator must produce at least one continuation; without "
        "one the continuation collapse is never exercised"
    )
