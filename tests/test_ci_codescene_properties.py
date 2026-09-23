"""Properties of the CodeScene boundary readers over generated inputs.

The example tests name the shapes a reviewer thought of. These generate the
space those shapes come from: nested documents with a marker at an arbitrary
depth, workflow call graphs of arbitrary shape including cycles, and guard
expressions with arbitrary spacing.
"""

from __future__ import annotations

import pathlib as pth
import tempfile

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.helpers.ci_closure import PULL_REQUEST_EVENTS, reachable
from tests.helpers.ci_codescene import contact_findings, guard_conjuncts, token_findings

#: Keys that name nothing the walk looks for.
_KEYS = st.sampled_from(["jobs", "steps", "with", "env", "run", "a", "b"])
#: Leaves that name nothing the walk looks for.
_LEAVES = st.sampled_from(["", "echo hi", "ubuntu-latest", "${{ github.ref }}"])


def _bury(marker: str) -> st.SearchStrategy[object]:
    """Generate a document holding ``marker`` at one arbitrary depth."""
    return st.recursive(
        st.just(marker),
        lambda inner: st.one_of(
            st.dictionaries(_KEYS, _LEAVES, max_size=3).flatmap(
                lambda siblings: st.tuples(_KEYS, inner).map(
                    lambda pair: {**siblings, pair[0]: pair[1]}
                )
            ),
            st.lists(_LEAVES, max_size=3).flatmap(
                lambda siblings: inner.map(lambda child: [*siblings, child])
            ),
        ),
        max_leaves=8,
    )


@given(document=_bury("curl ${{ secrets.CS_ACCESS_TOKEN }} https://api.codescene.io"))
def test_a_marker_at_any_depth_is_found(document: object) -> None:
    """The walk visits every string, however deeply it is nested."""
    assert token_findings({"x.yml": document}), f"missed the token in {document!r}"
    assert contact_findings({"x.yml": document}), f"missed the host in {document!r}"


@given(document=_bury("echo clean"))
def test_a_clean_document_has_no_findings(document: object) -> None:
    """The narrow half: nothing is found where nothing is named."""
    assert token_findings({"x.yml": document}) == [], document
    assert contact_findings({"x.yml": document}) == [], document


_NODES = 5
_EDGES = st.lists(
    st.tuples(st.integers(0, _NODES - 1), st.integers(0, _NODES - 1)), max_size=12
)


def _closure(edges: list[tuple[int, int]]) -> set[int]:
    """Return the nodes reachable from node 0 by the reference graph walk."""
    seen = {0}
    frontier = [0]
    while frontier:
        node = frontier.pop()
        for source, target in edges:
            if source == node and target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


@settings(max_examples=60, deadline=None)
@given(edges=_EDGES)
def test_reachable_is_the_transitive_closure(edges: list[tuple[int, int]]) -> None:
    """Any call graph, cycles included: the reader equals the graph closure.

    Node 0 answers `pull_request`; every other node declares only
    `workflow_call`, so it is reached exactly when a chain of calls from node 0
    leads to it.
    """
    with tempfile.TemporaryDirectory() as scratch:
        directory = pth.Path(scratch)
        for node in range(_NODES):
            trigger = "pull_request" if node == 0 else "workflow_call"
            calls = sorted({target for source, target in edges if source == node})
            jobs = "".join(
                f"  c{target}:\n    uses: ./.github/workflows/w{target}.yml\n"
                for target in calls
            )
            (directory / f"w{node}.yml").write_text(
                f"on: {trigger}\njobs:\n{jobs or '  {}'}\n".replace(
                    "jobs:\n  {}", "jobs: {}"
                ),
                encoding="utf-8",
            )
        reached = reachable(PULL_REQUEST_EVENTS, directory)
    expected = {f"w{node}.yml" for node in _closure(edges)}
    assert set(reached) == expected, (edges, sorted(reached), sorted(expected))


_SPACE = st.text(alphabet=" \t\n", max_size=3)


@given(before=_SPACE, middle=_SPACE, after=_SPACE)
def test_spacing_never_changes_the_conjuncts(
    before: str, middle: str, after: str
) -> None:
    """Whitespace around and inside the operator is normalized away."""
    guard = f"${{{{{before}a == 'x'{middle}&&{after}b{before}}}}}"
    assert guard_conjuncts(guard) == {"a == 'x'", "b"}, guard


@given(left=_SPACE, right=_SPACE)
def test_an_unquoted_alternative_is_always_refused(left: str, right: str) -> None:
    """``||`` outside a literal is refused wherever it sits."""
    guard = f"a == 'x' && b{left}||{right}c"
    with pytest.raises(AssertionError, match=r"must not contain \|\|"):
        guard_conjuncts(guard)
