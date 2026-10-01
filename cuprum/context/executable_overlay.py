"""Pure composition of executable-binding layers.

An executable binding maps a logical :class:`~cuprum.program.Program` to the
executable that actually runs: a fixed validated path, or a zero-argument
resolver evaluated at spawn time. Bindings live in the same
:class:`~cuprum.context.CuprumContext` as every other scoped policy, so they
are narrowed, nested, and isolated exactly like the allowlist and the
environment overlay.

This module owns the overlay-only merge (:func:`merge_executable_bindings`)
and the coercion that snapshots a layer into an immutable mapping. Like
:mod:`cuprum.context.env_overlay` it has no ``ContextVar`` dependency and does
not import :mod:`cuprum.context.core`, so the dependency direction stays
acyclic. Unlike the environment overlay it renders nothing: bindings are read
at spawn time through
:meth:`~cuprum.context.CuprumContext.resolve_executable`.
"""

from __future__ import annotations

import typing as typ
from types import MappingProxyType

from cuprum.executable_binding import ExecutableBinding

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.program import Program

type ExecutableBindingOverlay = cabc.Mapping[Program, ExecutableBinding]
"""A read-only layer mapping logical programs to their executable bindings."""


def _coerce_executable_bindings(
    bindings: ExecutableBindingOverlay | None,
) -> ExecutableBindingOverlay | None:
    """Return an immutable snapshot of ``bindings``, or ``None``."""
    if bindings is None:
        return None
    return MappingProxyType(dict(bindings))


def merge_executable_bindings(
    parent: ExecutableBindingOverlay | None,
    child: ExecutableBindingOverlay | None,
) -> ExecutableBindingOverlay | None:
    """Layer ``child`` over ``parent``; ``None`` means *inherit unchanged*.

    The child layer wins key by key: an inner scope can rebind a program the
    outer scope already bound, and can add bindings the outer scope never
    mentioned. Composition never copies a binding — entries move between
    layers by identity, so a resolver stays the same callable wherever it is
    read from.

    Parameters
    ----------
    parent : collections.abc.Mapping[Program, ExecutableBinding] | None
        The base layer. ``None`` means *inherit unchanged* — the layer
        contributes nothing.
    child : collections.abc.Mapping[Program, ExecutableBinding] | None
        The layer applied over ``parent``; its bindings win on key
        collisions. ``None`` means *inherit unchanged*.

    Returns
    -------
    collections.abc.Mapping[Program, ExecutableBinding] | None
        An immutable snapshot of the composed layer, or ``None`` when both
        layers are ``None``, meaning *inherit the effective bindings
        unchanged*.

    Examples
    --------
    >>> merged = merge_executable_bindings({"echo": "outer"}, {"echo": "inner"})
    >>> merged["echo"]
    'inner'
    """
    if parent is None and child is None:
        return None
    if parent is None:
        return _coerce_executable_bindings(child)
    if child is None:
        # Snapshot rather than return ``parent`` itself: a caller may pass a
        # plain dict, and the composed layer must never alias a mutable object
        # the caller can still write to.
        return _coerce_executable_bindings(parent)
    merged = dict(parent)
    merged.update(child)
    return MappingProxyType(merged)


__all__ = [
    "ExecutableBindingOverlay",
    "merge_executable_bindings",
]
