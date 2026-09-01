"""Context and working-directory helpers for subprocess execution.

This module gathers the shared, context-aware utilities used when spawning
subprocesses, so the single-command and pipeline spawn paths stay in
agreement:

- ``_cwd_arg`` is the canonical conversion of an optional working directory
  into the ``cwd`` argument accepted by ``asyncio.create_subprocess_exec``,
  used by both the single-command and pipeline spawn sites.
- ``_resolve_timeout`` resolves the effective timeout from the explicit,
  per-call execution-context, and ambient scoped values, in that order.
- ``_sh_module`` and ``_current_context`` are lazy-import shims that break the
  circular imports between this module and ``cuprum.sh``/``cuprum.context``.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.context import CuprumContext
    from cuprum.sh import CommandResult, ExecutionContext, TimeoutExpired


class _ShModule(typ.Protocol):
    """Structural view of the ``cuprum.sh`` members reached lazily.

    Only the two constructors below are accessed through :func:`_sh_module`
    (``CommandResult`` in ``cuprum._subprocess_execution`` and
    ``TimeoutExpired`` in ``cuprum._subprocess_timeout``), so naming them
    keeps the lazy-import shim typed without reintroducing the import cycle.
    """

    CommandResult: type[CommandResult]
    TimeoutExpired: type[TimeoutExpired]


def _sh_module() -> _ShModule:
    """Lazy import sh module to avoid circular imports."""
    from cuprum import sh

    # ty models module attributes as read-only, so a module object never
    # matches a protocol structurally; the cast records the checked surface
    # instead of widening the return type to ``Any``.
    return typ.cast("_ShModule", sh)


def _current_context() -> CuprumContext:
    """Get the current context via lazy import to avoid circular imports."""
    from cuprum.context import current_context

    return current_context()


def _cwd_arg(cwd: str | Path | None) -> str | None:
    """Return the ``cwd`` argument for ``asyncio.create_subprocess_exec``."""
    return str(cwd) if cwd is not None else None


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
