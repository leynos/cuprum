"""Pure environment-policy composition and rendering.

Overlay mappings are layered on top of the live ``os.environ`` at subprocess
spawn time. This module owns the overlay-only merge (:func:`merge_env_overlays`)
and the spawn-time resolution against the live environment
(:func:`resolve_env`); it has no ``ContextVar`` dependency.
"""

from __future__ import annotations

import enum
import os
import typing as typ
from types import MappingProxyType

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class EnvMode(enum.StrEnum):
    """Choose how a child environment relates to its parent environment.

    ``OVERLAY`` is the default and layers supplied values over the live parent
    environment. ``INHERIT`` preserves an inherited policy without adding a
    replacement boundary. ``REPLACE`` starts the child with an empty
    environment before applying its supplied values.
    """

    INHERIT = "inherit"
    OVERLAY = "overlay"
    REPLACE = "replace"


@typ.final
class UnsetType:
    """Singleton marker requesting removal of an environment variable."""

    __slots__ = ()
    _instance: typ.ClassVar[UnsetType | None] = None

    def __new__(cls) -> typ.Self:
        """Return the one unset marker instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return typ.cast("typ.Self", cls._instance)

    def __repr__(self) -> str:
        """Return the marker's public spelling."""
        return "UNSET"


UNSET = UnsetType()

type EnvOverlayValue = str | UnsetType
type EnvOverlay = cabc.Mapping[str, EnvOverlayValue]


def _coerce_env_overlay(
    overlay: EnvOverlay | None,
) -> EnvOverlay | None:
    """Return an immutable env-overlay snapshot, or ``None``."""
    if overlay is None:
        return None
    return MappingProxyType(dict(overlay))


def merge_env_overlays(
    parent: EnvOverlay | None,
    child: EnvOverlay | None,
) -> EnvOverlay | None:
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
    parent : collections.abc.Mapping[str, str | UnsetType] | None
        The base overlay layer. ``None`` means *inherit unchanged* — the
        layer contributes nothing.
    child : collections.abc.Mapping[str, str | UnsetType] | None
        The overlay layered on top of ``parent``; its values win on key
        collisions. ``None`` means *inherit unchanged*.

    Returns
    -------
    collections.abc.Mapping[str, str | UnsetType] | None
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


def render_env(
    overlay: EnvOverlay | None,
    mode: EnvMode = EnvMode.OVERLAY,
) -> dict[str, str] | None:
    """Render a composed environment policy for subprocess spawning.

    ``REPLACE`` starts from an empty environment. Every other mode starts
    from a live copy of :data:`os.environ`. ``UNSET`` values remove keys only
    from the rendered child mapping and never mutate process-global state.

    Returns
    -------
    dict[str, str] | None
        The environment mapping to pass to the child, or ``None`` to inherit.
    """
    if mode is not EnvMode.REPLACE and not overlay:
        return None

    rendered = {} if mode is EnvMode.REPLACE else os.environ.copy()
    if overlay is None:
        return rendered

    for key, value in overlay.items():
        if isinstance(value, UnsetType):
            rendered.pop(key, None)
        else:
            rendered[key] = value
    return rendered


def resolve_env(
    *layers: EnvOverlay | None,
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
    overlay: EnvOverlay | None = None
    for layer in layers:
        overlay = merge_env_overlays(overlay, layer)
    return render_env(overlay)


__all__ = [
    "UNSET",
    "EnvMode",
    "UnsetType",
    "merge_env_overlays",
    "render_env",
    "resolve_env",
]
