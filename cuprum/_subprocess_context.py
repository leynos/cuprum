"""Context and working-directory helpers for subprocess execution.

This module gathers the shared, context-aware utilities used when spawning
subprocesses, so the single-command and pipeline spawn paths stay in
agreement:

- ``_cwd_arg`` is the canonical conversion of an optional working directory
  into the ``cwd`` argument accepted by ``asyncio.create_subprocess_exec``,
  used by both the single-command and pipeline spawn sites.
- ``_ownership_spawn_kwargs`` and ``_owns_process_group`` turn a
  ``ProcessGroupPolicy`` into the spawn keyword it implies and the fact it
  records, and are the only place either decision is made.
- ``_resolve_timeout`` resolves the effective timeout from the explicit,
  per-call execution-context, and ambient scoped values, in that order.
- ``_sh_module`` and ``_current_context`` are lazy-import shims that break the
  circular imports between this module and ``cuprum.sh``/``cuprum.context``.
"""

from __future__ import annotations

import os
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.context import CuprumContext
    from cuprum.sh import CommandResult, ExecutionContext, TimeoutExpired

# Imported at runtime, not under ``TYPE_CHECKING``: the enum is the parameter
# type of the helpers below and the value the platform guard compares against,
# so the name must resolve whenever either is called. ``cuprum.sh.execution``
# imports only ``cuprum.context``, so this does not close a cycle back into the
# module that imports these helpers.
from cuprum.sh.execution import ProcessGroupPolicy


class _OwnershipSpawnKwargs(typ.TypedDict, total=False):
    """Spawn keyword arguments a process-group policy implies."""

    start_new_session: bool


def _cwd_arg(cwd: str | Path | None) -> str | None:
    """Return the ``cwd`` argument for ``asyncio.create_subprocess_exec``."""
    return str(cwd) if cwd is not None else None


def _owns_process_group(policy: ProcessGroupPolicy) -> bool:
    """Report whether ``policy`` asks the run to own its child's group."""
    return policy is ProcessGroupPolicy.OWN_GROUP


def _ownership_spawn_kwargs(
    policy: ProcessGroupPolicy,
) -> _OwnershipSpawnKwargs:
    """Return the spawn keywords ``policy`` implies for a child process.

    ``INHERIT`` implies none, leaving the child in the parent's process group
    and session exactly as before. ``OWN_GROUP`` implies ``start_new_session``,
    which makes the child the leader of a new session and process group on
    POSIX.

    Returns
    -------
    _OwnershipSpawnKwargs
        The keyword arguments to pass to the spawn call, empty for
        ``INHERIT``.

    Raises
    ------
    ValueError
        If ``OWN_GROUP`` is requested where POSIX process groups do not exist.
        Windows is the case that matters: it would need a Job Object, which
        cannot be assigned atomically with process creation, so a child could
        spawn a descendant before containment began. Refusing is honest;
        accepting the option and delivering no containment would not be.
    """
    if not _owns_process_group(policy):
        return {}
    if os.name != "posix":
        msg = (
            "ProcessGroupPolicy.OWN_GROUP requires POSIX process groups, "
            f"which are unavailable on this platform (os.name={os.name!r})"
        )
        raise ValueError(msg)
    # The child becomes a session and process-group leader, so its group is
    # addressed by its own process identifier: no lookup is needed, and the
    # identifier can never name the caller's own group.
    return {"start_new_session": True}


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
