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
- ``_event_cwd`` renders the directory an *event* reports, and
  ``_execution_cwd`` the absolute base a *relative* executable binding is
  anchored at; the two are kept apart because only resolution needs the
  absolute form.
- ``_resolve_timeout`` resolves the effective timeout from the explicit,
  per-call execution-context, and ambient scoped values, in that order.
- ``_sh_module`` and ``_current_context`` are lazy-import shims that break the
  circular imports between this module and ``cuprum.sh``/``cuprum.context``.
"""

from __future__ import annotations

import os
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    from cuprum.context import CuprumContext
    from cuprum.sh import (
        BytesCommandResult,
        CommandResult,
        ExecutionContext,
        TimeoutExpired,
    )

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

    Only the constructors below are accessed through :func:`_sh_module`:
    ``CommandResult`` and ``BytesCommandResult`` in
    ``cuprum._subprocess_execution``, where the run's mode picks between them,
    and ``TimeoutExpired`` in ``cuprum._subprocess_timeout``. Naming them keeps
    the lazy-import shim typed without reintroducing the import cycle.
    """

    CommandResult: type[CommandResult]
    BytesCommandResult: type[BytesCommandResult]
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


def _event_cwd(cwd: str | Path | None) -> Path | None:
    """Return the working directory an *event* reports: the configured one.

    Both observation builders record the run's directory on ``ExecEvent.cwd``
    (and, through the adapters, as ``cuprum_cwd`` / ``cuprum.cwd``). That field
    is a report of what the caller configured, not a resolution input, so it is
    never anchored against the process directory. Anchoring it here would make
    the event disagree with both the caller and the spawn — the spawn receives
    the same directory through :func:`_cwd_arg`.

    "Configured directory" means the value the caller passed, represented as a
    :class:`~pathlib.Path` — not a verbatim copy of their string. ``Path``
    normalizes lexical detail such as a trailing ``/`` or a ``./`` segment, so
    this is the caller's *directory*, faithfully, rather than their spelling of
    it; a caller comparing strings should not expect their bytes back.

    Only *resolution* needs the absolute form, which is what
    :func:`_execution_cwd` exists for. Keeping the two side by side states the
    distinction once, where a reader comparing them will find it.

    Returns
    -------
    Path | None
        The caller's directory as configured, or ``None`` when they named none.
    """
    return None if cwd is None else Path(cwd)


def _execution_cwd(cwd: str | Path | None) -> Path | None:
    """Return the directory a *relative* binding may be anchored at, absolutely.

    A caller may supply the working directory relative to the process's own, and
    a relative binding is anchored at it. Composing those naively yields a
    relative ``argv[0]``, which the child then resolves against the very
    directory it was already anchored to — applying the same prefix a second
    time. The child would execute one file while ``resolved_path`` named
    another, disagreeing exactly where a caller audits which file ran.

    Anchoring here makes the composed path absolute, so it is anchored once, by
    us, and the child cannot re-anchor it. The spawn keeps its own spelling of
    the same directory, which is what ``_cwd_arg`` already renders. ``""`` falls
    through as any relative spelling does — ``Path("")`` is ``Path(".")`` — and
    that answer is unobservable because ``asyncio`` refuses an empty ``cwd``
    before a child starts; it is left as it falls rather than mapped to ``None``,
    which would claim the caller named no directory when they named one.

    Returns
    -------
    Path | None
        The absolute anchoring base, or ``None`` when *cwd* is ``None``, since
        an unspecified working directory means the process's own and both
        consumers already apply that.
    """
    if cwd is None:
        return None
    path = Path(cwd)
    if path.is_absolute():
        return path
    return Path.cwd() / path


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
