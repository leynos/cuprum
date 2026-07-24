"""Pure environment-overlay merging for scoped execution contexts.

Overlay mappings are layered on top of the live ``os.environ`` at subprocess
spawn time. This module owns the overlay-only merge (:func:`merge_env_overlays`)
and the spawn-time resolution against the live environment
(:func:`resolve_env`); it has no ``ContextVar`` dependency.
"""

from __future__ import annotations

import os
import typing as typ
from types import MappingProxyType

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _coerce_env_overlay(
    overlay: cabc.Mapping[str, str] | None,
) -> cabc.Mapping[str, str] | None:
    """Return an immutable env-overlay snapshot, or ``None``."""
    if overlay is None:
        return None
    return MappingProxyType(dict(overlay))


def merge_env_overlays(
    parent: cabc.Mapping[str, str] | None,
    child: cabc.Mapping[str, str] | None,
) -> cabc.Mapping[str, str] | None:
    """Layer ``child`` over ``parent``; ``None`` means *inherit unchanged*.

    Both layers are kept overlay-only — they never include a snapshot of
    :func:`os.environ`. The live process environment is read at spawn time so
    that callers can monkey-patch or otherwise mutate ``os.environ`` after
    Cuprum has been imported and still have those updates visible to
    subprocesses spawned inside the scope.

    Exposed as public API so that other cuprum modules — and downstream
    code that builds custom observation tags — can merge overlay layers
    without reaching for a private symbol.

    Parameters
    ----------
    parent : collections.abc.Mapping[str, str] | None
        The base overlay layer. ``None`` means *inherit unchanged* — the
        layer contributes nothing.
    child : collections.abc.Mapping[str, str] | None
        The overlay layered on top of ``parent``; its values win on key
        collisions. ``None`` means *inherit unchanged*.

    Returns
    -------
    collections.abc.Mapping[str, str] | None
        An immutable snapshot of the merged overlay, or ``None`` when both
        layers are ``None`` (meaning *inherit the environment unchanged*).
    """
    if parent is None and child is None:
        return None
    if parent is None:
        return _coerce_env_overlay(child)
    if child is None:
        # Even if ``parent`` is already a MappingProxyType from a prior
        # coerce, callers may pass a plain dict; return an immutable
        # snapshot so the result never aliases a caller-mutable object.
        return _coerce_env_overlay(parent)
    merged = dict(parent)
    merged.update(child)
    return MappingProxyType(merged)


def resolve_env(
    *layers: cabc.Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Merge ``os.environ`` (read live) with the supplied overlay layers.

    Layers are applied left-to-right; later values win. ``None`` *and empty*
    layers are skipped — an empty overlay contributes nothing, so treating it
    as a no-op avoids an unnecessary copy of :func:`os.environ` and lets the
    subprocess inherit the parent environment directly. When every layer is
    skipped the function returns ``None`` so callers may pass it through to
    ``subprocess`` APIs to mean *inherit the parent environment unchanged*.

    The call to :func:`os.environ.copy` is deferred until at least one
    overlay is non-empty, so the result reflects whatever the process
    environment looks like at the moment of resolution — the exact behaviour
    the issue requires.

    Parameters
    ----------
    *layers : collections.abc.Mapping[str, str] | None
        Overlay layers applied left-to-right over a live copy of
        ``os.environ``; later values win. ``None`` and empty layers are
        skipped.

    Returns
    -------
    dict[str, str] | None
        The merged environment mapping, or ``None`` when every layer is
        ``None`` or empty (meaning *inherit the parent environment
        unchanged*).
    """
    if all(not layer for layer in layers):
        return None
    merged: dict[str, str] = os.environ.copy()
    for layer in layers:
        if not layer:
            continue
        merged.update(layer)
    return merged


__all__ = [
    "merge_env_overlays",
    "resolve_env",
]
