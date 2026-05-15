"""Context helpers for subprocess execution."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.context import CuprumContext
    from cuprum.sh import ExecutionContext


def _sh_module() -> typ.Any:  # noqa: ANN401 — returns module, typed access via attributes
    """Lazy import sh module to avoid circular imports."""
    from cuprum import sh

    return sh


def _current_context() -> CuprumContext:
    """Get the current context via lazy import to avoid circular imports."""
    from cuprum.context import current_context

    return current_context()


def _resolve_timeout(
    *,
    timeout: float | None,
    context: ExecutionContext | None,
) -> float | None:
    """Resolve the effective timeout from explicit, context, and scoped values."""
    if timeout is not None:
        return timeout
    if context is not None and context.timeout is not None:
        return context.timeout
    return _current_context().timeout
