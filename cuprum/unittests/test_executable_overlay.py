"""Named examples for executable-binding overlay composition.

``merge_executable_bindings`` is the scope-composition rule for executable
bindings, mirroring :func:`cuprum.context.merge_env_overlays`: ``None`` means
*inherit unchanged*, a child mapping wins key by key, and the result is an
immutable snapshot rather than a view of a caller's mutable mapping. These
examples pin those three rules and the snapshot property that keeps a later
caller mutation from silently rewriting a context.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum.catalogue import ECHO, LS
from cuprum.context.executable_overlay import merge_executable_bindings
from cuprum.executable_binding import executable_binding
from cuprum.program import Program

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_ECHO_BINDING = executable_binding(ECHO, "/opt/tools/echo")
_LS_BINDING = executable_binding(LS, "/opt/tools/ls")


def test_both_layers_none_means_inherit_unchanged() -> None:
    """Two absent layers leave the inherited policy unspecified."""
    assert merge_executable_bindings(None, None) is None


def test_parent_only_layer_is_preserved() -> None:
    """A layer with no child contributes its own keys."""
    merged = merge_executable_bindings({ECHO: _ECHO_BINDING}, None)
    assert merged == {ECHO: _ECHO_BINDING}, "the parent layer must survive"


def test_child_only_layer_is_preserved() -> None:
    """A layer with no parent contributes its own keys."""
    merged = merge_executable_bindings(None, {ECHO: _ECHO_BINDING})
    assert merged == {ECHO: _ECHO_BINDING}, "the child layer must survive"


def test_child_wins_on_key_collision() -> None:
    """The innermost binding for a program is the one that takes effect."""
    parent = {ECHO: _ECHO_BINDING}
    child = {ECHO: _LS_BINDING}
    merged = merge_executable_bindings(parent, child)
    assert merged is not None
    assert merged[ECHO] is _LS_BINDING, "the child binding must win the collision"


def test_distinct_programs_are_unioned() -> None:
    """Bindings for different programs compose rather than replace."""
    merged = merge_executable_bindings(
        {ECHO: _ECHO_BINDING},
        {LS: _LS_BINDING},
    )
    assert merged == {ECHO: _ECHO_BINDING, LS: _LS_BINDING}, (
        "a distinct key must not displace the parent's entries"
    )


def test_merge_does_not_mutate_either_layer() -> None:
    """Composition leaves the caller's mappings exactly as they were."""
    parent = {ECHO: _ECHO_BINDING}
    child = {ECHO: _LS_BINDING, Program("tar"): _LS_BINDING}
    parent_before = dict(parent)
    child_before = dict(child)
    merge_executable_bindings(parent, child)
    assert parent == parent_before, "the parent layer must not be mutated"
    assert child == child_before, "the child layer must not be mutated"


def test_merge_result_rejects_item_assignment() -> None:
    """The composed layer is read-only, so no caller can reopen it."""
    merged = merge_executable_bindings({ECHO: _ECHO_BINDING}, {LS: _LS_BINDING})
    assert merged is not None
    with pytest.raises(TypeError):
        typ.cast("cabc.MutableMapping[Program, object]", merged)[ECHO] = _LS_BINDING


def test_merge_snapshots_a_mutable_parent() -> None:
    """Mutating a layer after composition cannot change the composed result.

    Without the snapshot, a scope that merged a caller-owned dict would keep
    observing later writes to it, so a binding could appear or vanish at a
    distance from the code that installed it.
    """
    parent: dict[Program, object] = {ECHO: _ECHO_BINDING}
    merged = merge_executable_bindings(typ.cast("typ.Any", parent), None)
    parent.clear()
    assert merged == {ECHO: _ECHO_BINDING}, (
        "the composed layer must not alias the caller's mapping"
    )


def test_merge_snapshots_a_mutable_child() -> None:
    """A child layer is snapshotted on the same terms as the parent."""
    child: dict[Program, object] = {LS: _LS_BINDING}
    merged = merge_executable_bindings(None, typ.cast("typ.Any", child))
    child.clear()
    assert merged == {LS: _LS_BINDING}, (
        "the composed layer must not alias the child's mapping"
    )


def test_merge_of_two_populated_layers_is_a_new_mapping() -> None:
    """Composition allocates, so neither input is returned as the result."""
    parent = {ECHO: _ECHO_BINDING}
    child = {LS: _LS_BINDING}
    merged = merge_executable_bindings(parent, child)
    assert merged is not parent
    assert merged is not child


def test_merge_is_associative_over_three_layers() -> None:
    """Grouping layers does not change the effective bindings."""
    outer = {ECHO: _ECHO_BINDING}
    middle = {ECHO: _LS_BINDING, LS: _LS_BINDING}
    inner = {ECHO: _ECHO_BINDING}

    left = merge_executable_bindings(merge_executable_bindings(outer, middle), inner)
    right = merge_executable_bindings(outer, merge_executable_bindings(middle, inner))
    assert left == right, "nested scope composition must be associative"


def test_merge_leaves_binding_identity_intact() -> None:
    """Composition moves bindings between layers without copying them."""
    merged = merge_executable_bindings({ECHO: _ECHO_BINDING}, None)
    assert merged is not None
    assert merged[ECHO] is _ECHO_BINDING, (
        "a binding must survive composition by identity, not by value copy"
    )


def test_child_does_not_win_a_key_it_never_mentions() -> None:
    """Only colliding keys are overridden; the rest inherit."""
    parent = {ECHO: _ECHO_BINDING, LS: _LS_BINDING}
    merged = merge_executable_bindings(parent, {ECHO: _LS_BINDING})
    assert merged is not None
    assert merged[LS] is _LS_BINDING, "an unmentioned parent key must inherit"


@pytest.mark.parametrize("layer", ["parent", "child"])
def test_a_single_entry_layer_composes_against_none(layer: str) -> None:
    """Either side of the merge tolerates an absent counterpart."""
    entry = {ECHO: _ECHO_BINDING}
    merged = (
        merge_executable_bindings(entry, None)
        if layer == "parent"
        else merge_executable_bindings(None, entry)
    )
    assert merged == entry, f"the {layer} layer must compose with an absent peer"
