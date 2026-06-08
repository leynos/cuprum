"""Property-based tests for env overlay helpers.

This module pins down the merge and resolve invariants that
``cuprum.context.merge_env_overlays`` and ``cuprum.context.resolve_env``
must hold over arbitrary layer counts and payload contents. The helpers
are pure and accept any string-to-string mapping, so Hypothesis can
explore the input domain without spinning up subprocesses.

The invariants checked here are:

- ``merge_env_overlays``: left-to-right precedence (child wins),
  associativity of the layered merge against ``resolve_env``,
  immutability of the returned proxy, and that caller-supplied mappings
  are never mutated.
- ``resolve_env``: every non-empty layer contributes, later layers win
  over earlier ones, empty and ``None`` layers are skipped, and the
  returned dict reflects the *live* ``os.environ`` at the moment of
  resolution.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum.context import merge_env_overlays, resolve_env

# Hypothesis strategy: env-var-style names ("[A-Z_][A-Z0-9_]*") with a small
# alphabet of values. Keeping the namespace bounded lets layers actually
# overlap and exercises the "later wins" rule.
_NAMES = st.text(
    alphabet="ABCDE_",
    min_size=1,
    max_size=4,
).filter(lambda s: not s[0].isdigit())
_VALUES = st.text(alphabet="xyz0123-", min_size=0, max_size=6)
_OVERLAYS = st.dictionaries(_NAMES, _VALUES, max_size=6)
_OPTIONAL_OVERLAYS = st.one_of(st.none(), _OVERLAYS)


@settings(max_examples=200)
@given(parent=_OPTIONAL_OVERLAYS, child=_OPTIONAL_OVERLAYS)
def test_merge_env_overlays_child_wins(
    parent: dict[str, str] | None,
    child: dict[str, str] | None,
) -> None:
    """For every key in ``child``, the merged overlay carries ``child``'s value.

    This is the core precedence invariant of the overlay merge: ``child`` is
    layered on top of ``parent`` and therefore wins on every key it defines.
    """
    merged = merge_env_overlays(parent, child)
    if child:
        assert merged is not None
        for key, value in child.items():
            assert merged[key] == value


@settings(max_examples=200)
@given(parent=_OPTIONAL_OVERLAYS, child=_OPTIONAL_OVERLAYS)
def test_merge_env_overlays_keeps_parent_when_child_silent(
    parent: dict[str, str] | None,
    child: dict[str, str] | None,
) -> None:
    """Keys present in ``parent`` but not ``child`` survive unchanged."""
    merged = merge_env_overlays(parent, child)
    if not parent:
        return
    for key, value in parent.items():
        if child is not None and key in child:
            continue
        assert merged is not None
        assert merged[key] == value


@settings(max_examples=100)
@given(parent=_OPTIONAL_OVERLAYS, child=_OPTIONAL_OVERLAYS)
def test_merge_env_overlays_does_not_mutate_inputs(
    parent: dict[str, str] | None,
    child: dict[str, str] | None,
) -> None:
    """``merge_env_overlays`` never mutates the caller-supplied mappings."""
    parent_snapshot = None if parent is None else dict(parent)
    child_snapshot = None if child is None else dict(child)
    merge_env_overlays(parent, child)
    assert (None if parent is None else dict(parent)) == parent_snapshot
    assert (None if child is None else dict(child)) == child_snapshot


@settings(max_examples=100)
@given(parent=_OVERLAYS, child=_OVERLAYS)
def test_merge_env_overlays_result_is_immutable_proxy(
    parent: dict[str, str],
    child: dict[str, str],
) -> None:
    """The returned mapping is read-only when at least one layer is non-empty."""
    merged = merge_env_overlays(parent, child)
    if not parent and not child:
        # Both layers empty: helper short-circuits to one of them; that
        # branch is covered by the unit tests, not the property suite.
        return
    assert merged is not None
    with pytest.raises(TypeError):
        merged["__cuprum_property_test__"] = "no"  # type: ignore[index]


@settings(max_examples=100)
@given(
    layers=st.lists(_OPTIONAL_OVERLAYS, min_size=0, max_size=5),
)
def test_resolve_env_layers_apply_left_to_right(
    layers: list[dict[str, str] | None],
) -> None:
    """``resolve_env`` resolves layers so the last writer of each key wins."""
    expected: dict[str, str] = {}
    non_empty = [layer for layer in layers if layer]
    for layer in non_empty:
        expected.update(layer)

    merged = resolve_env(*layers)
    if not non_empty:
        assert merged is None
        return

    assert merged is not None
    # Every overlay key is present with the last-writer value.
    for key, value in expected.items():
        assert merged[key] == value
    # The live ``os.environ`` is preserved for keys no layer mentions.
    for key in os.environ.keys() - expected.keys():
        assert merged[key] == os.environ[key]


@settings(max_examples=100)
@given(
    layers=st.lists(_OPTIONAL_OVERLAYS, min_size=0, max_size=5),
)
def test_resolve_env_empty_layers_are_skipped(
    layers: list[dict[str, str] | None],
) -> None:
    """Empty mappings and ``None`` are equivalent to *not contributing*."""
    augmented: list[dict[str, str] | None] = []
    for layer in layers:
        augmented.extend(({}, None, layer))
    assert resolve_env(*layers) == resolve_env(*augmented)


@settings(max_examples=50)
@given(parent=_OVERLAYS, child=_OVERLAYS)
def test_resolve_env_equivalent_to_merge_plus_os_environ(
    parent: dict[str, str],
    child: dict[str, str],
) -> None:
    """``resolve_env(parent, child)`` equals ``os.environ`` updated by the merge.

    This is the associativity rule that ties the two helpers together:
    composing through ``merge_env_overlays`` first, then resolving once,
    must yield the same result as resolving in one pass.
    """
    direct = resolve_env(parent, child)
    via_merge = resolve_env(merge_env_overlays(parent, child))
    assert direct == via_merge
