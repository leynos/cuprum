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
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum.context import (
    UNSET,
    EnvMode,
    UnsetType,
    merge_env_overlays,
    resolve_env,
)
from cuprum.context._policy import _resolve_env_policy
from cuprum.context.env_overlay import render_env

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
_POLICY_VALUES = st.one_of(_VALUES, st.just(UNSET))
_POLICY_OVERLAYS = st.dictionaries(_NAMES, _POLICY_VALUES, max_size=6)
_OPTIONAL_POLICY_OVERLAYS = st.one_of(st.none(), _POLICY_OVERLAYS)


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
    # Cast away the read-only static type to exercise the runtime guard.
    with pytest.raises(TypeError):
        typ.cast("dict[str, str]", merged)["__cuprum_property_test__"] = "no"


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


@settings(max_examples=150)
@given(
    parent=_OPTIONAL_POLICY_OVERLAYS,
    child=_OPTIONAL_POLICY_OVERLAYS,
    parent_mode=st.sampled_from(tuple(EnvMode)),
    child_mode=st.sampled_from(tuple(EnvMode)),
)
def test_resolve_env_policy_composes_modes_and_unset_markers(
    parent: dict[str, str | UnsetType] | None,
    child: dict[str, str | UnsetType] | None,
    parent_mode: EnvMode,
    child_mode: EnvMode,
) -> None:
    """Composition preserves markers and grants replacement its fresh boundary."""
    overlay, mode = _resolve_env_policy(parent, parent_mode, child, child_mode)
    if child_mode is EnvMode.REPLACE:
        assert mode is EnvMode.REPLACE, (
            "a replacement child must select replacement rendering"
        )
        assert dict(overlay or {}) == dict(child or {}), (
            "a nested replacement must discard its parent overlay"
        )
        return

    assert mode is parent_mode, "overlay and inherit children preserve parent mode"
    expected = dict(parent or {})
    expected.update(child or {})
    assert dict(overlay or {}) == expected, (
        "overlay composition must retain UNSET markers until render time"
    )


@pytest.mark.parametrize(
    ("parent_mode", "child_mode"),
    [(None, EnvMode.OVERLAY), (EnvMode.OVERLAY, "replace")],
)
def test_resolve_env_policy_rejects_invalid_modes(
    parent_mode: object,
    child_mode: object,
) -> None:
    """Only typed ``EnvMode`` values may control environment rendering."""
    with pytest.raises(TypeError, match="environment modes must be EnvMode values"):
        _resolve_env_policy(
            None,
            typ.cast("EnvMode", parent_mode),
            None,
            typ.cast("EnvMode", child_mode),
        )


@pytest.mark.parametrize("invalid_mode", [None, "replace"])
def test_render_env_rejects_invalid_modes(invalid_mode: object) -> None:
    """Rendering accepts only typed ``EnvMode`` policy values."""
    with pytest.raises(TypeError, match="environment mode must be an EnvMode value"):
        render_env(None, typ.cast("EnvMode", invalid_mode))
