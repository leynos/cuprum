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
    from cuprum.sh import ExecutionContext


def _sh_module() -> typ.Any:  # noqa: ANN401 — returns module, typed access via attributes
    """Lazy import sh module to avoid circular imports."""
    from cuprum import sh

    return sh


def _current_context() -> CuprumContext:
    """Get the current context via lazy import to avoid circular imports."""
    from cuprum.context import current_context

    return current_context()


def _cwd_arg(cwd: str | Path | None) -> str | None:
    """Return the ``cwd`` argument for ``asyncio.create_subprocess_exec``.

    Canonical conversion shared by the single-command and pipeline spawn
    sites so the optional working directory is rendered identically in both
    paths. A ``Path`` argument is rendered via ``str`` exactly as a string
    argument is; see ``test_cwd_arg_conversion`` for the exhaustive cases.

    Example
    -------
    >>> _cwd_arg(None) is None
    True
    >>> _cwd_arg("/srv/data")
    '/srv/data'
    """
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
