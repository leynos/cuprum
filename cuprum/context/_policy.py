"""Pure execution-context policy helpers.

Low-level, side-effect-free helpers that implement the context narrowing and
validation policy shared by :class:`cuprum.context.core.CuprumContext` and
:class:`~cuprum.context.core.ScopeConfig`: allowlist narrowing and
restriction tracking, timeout validation and resolution, and generic hook
merging.

This module is deliberately dependency-light. It must not import
``CuprumContext``, the registration handles, or the ``ContextVar`` state
plumbing so the dependency direction stays acyclic: ``core`` (and the wider
``cuprum.context`` package) depend on it, never the reverse.
"""

from __future__ import annotations

import math
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.program import Program


def _validate_timeout(timeout: float | None, class_name: str) -> float | None:
    """Validate a timeout is finite and non-negative, coercing it to float."""
    if timeout is None:
        return None
    try:
        timeout_float = float(timeout)
    except OverflowError as exc:
        msg = f"{class_name} timeout must be non-negative, got an unrepresentable value"
        raise ValueError(msg) from exc
    if not math.isfinite(timeout_float):
        msg = f"{class_name} timeout must be finite, got {timeout_float}"
        raise ValueError(msg)
    if timeout_float < 0:
        msg = f"{class_name} timeout must be non-negative, got {timeout_float}"
        raise ValueError(msg)
    return timeout_float


def _narrow_allowlist(
    parent: frozenset[Program],
    config: frozenset[Program] | None,
    *,
    parent_is_restricted: bool = False,
) -> frozenset[Program]:
    """Return the allowlist produced by narrowing a parent context."""
    if config is None:
        return parent
    if parent_is_restricted and not parent:
        return parent
    if parent:
        return parent & config
    return config


def _is_narrowed_allowlist_restricted(
    config: frozenset[Program] | None,
    *,
    parent_is_restricted: bool,
) -> bool:
    """Return whether a narrowed context has an active allowlist restriction."""
    return parent_is_restricted or config is not None


def _merge_hooks[HookT](
    parent: tuple[HookT, ...],
    config: tuple[HookT, ...],
    *,
    scoped_first: bool,
) -> tuple[HookT, ...]:
    """Merge parent and scoped hooks using the requested scope ordering."""
    return config + parent if scoped_first else parent + config


def _resolve_narrowed_timeout(
    parent: float | None, config: float | None
) -> float | None:
    """Return the timeout inherited or overridden by a narrowed context."""
    if config is None:
        return parent
    return config
