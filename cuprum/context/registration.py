"""Scoped-context managers and registration handles.

Provides ``scoped`` plus the user-facing registration factories (``allow``,
``before``, ``after``, ``observe``, ``env``). All registration handles derive
from the canonical :class:`_TokenRegistration` base, which owns the
``ContextVar`` token-restoration discipline.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum.context.env_overlay import _coerce_env_overlay
from cuprum.context.state import _reset_context, _set_context, current_context

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from contextvars import Token

    from cuprum.context.core import (
        AfterHook,
        BeforeHook,
        CuprumContext,
        ScopeConfig,
    )
    from cuprum.events import ExecHook
    from cuprum.program import Program


class _ScopedContext:
    """Context manager for entering a scoped execution context."""

    __slots__ = ("_ctx", "_token")

    def __init__(self, config: ScopeConfig) -> None:
        """Narrow the current context with ``config`` for later entry."""
        parent = current_context()
        self._ctx = parent.narrow(config)
        self._token: Token[CuprumContext] | None = None

    def __enter__(self) -> CuprumContext:
        """Activate the scoped context and return it."""
        self._token = _set_context(self._ctx)
        return self._ctx

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Restore the previous context on scope exit."""
        if self._token is not None:
            _reset_context(self._token)


def scoped(config: ScopeConfig) -> _ScopedContext:
    """Create a scoped context manager for narrowed execution.

    Parameters
    ----------
    config:
        Scope configuration describing allowlist and hook updates.

    Returns
    -------
    _ScopedContext
        A context manager that narrows the current context.

    Example
    -------
    >>> with scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as ctx:
    ...     assert ctx.is_allowed(ECHO)

    """
    return _ScopedContext(config)


class _TokenRegistration:
    """Canonical base for ContextVar-backed scope-registration handles.

    All scope-registration handles (allowlist extensions, hook
    registrations, env overlays) derive from this base. Subclasses perform
    only the context-derivation step in ``__init__`` and hand the derived
    context to :meth:`_install`; the token capture, idempotent
    :meth:`detach`, and context-manager protocol live here so the subtle
    restoration discipline cannot drift between handle types.

    Token-based Restoration
    -----------------------
    The registration captures a :class:`~contextvars.Token` when the derived
    context is installed. When :meth:`detach` is called, the original context
    is restored via the token, ensuring no context pollution even when used
    outside ``scoped(ScopeConfig())`` blocks. This means :meth:`detach`
    restores the exact context that existed when the registration was
    created, regardless of subsequent context modifications. If multiple
    registrations are created and detached in non-LIFO (last in, first out)
    order, earlier tokens restore states that discard changes layered by
    later registrations; prefer ``with`` blocks, which detach in LIFO order.

    Detach in the same logical :class:`~contextvars.Context` (thread or
    task) in which the registration was created. Resetting a
    :class:`~contextvars.ContextVar` with a token from a different context
    raises :class:`ValueError`.
    """

    __slots__ = ("_detached", "_token")

    def __init__(self) -> None:
        """Initialise the handle in the attached, token-less state."""
        self._detached = False
        self._token: Token[CuprumContext] | None = None

    def _install(self, new_ctx: CuprumContext) -> None:
        """Set ``new_ctx`` as current and capture the restoration token."""
        self._token = _set_context(new_ctx)

    def detach(self) -> None:
        """Restore the original context via the captured token."""
        if self._detached:
            return
        self._detached = True
        if self._token is not None:
            _reset_context(self._token)
            self._token = None

    def __enter__(self) -> typ.Self:
        """Enter context manager; the registration is already installed."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit context manager; detach the registration."""
        self.detach()


class AllowRegistration(_TokenRegistration):
    """Registration handle for dynamic allowlist extension.

    Supports ``detach()`` and context-manager usage for scoped allowing. The
    token-restoration discipline is documented on
    :class:`_TokenRegistration`.
    """

    __slots__ = ("_programs",)

    def __init__(self, *programs: Program) -> None:
        """Create an allowlist registration and add programs to current context."""
        super().__init__()
        self._programs = frozenset(programs)
        ctx = current_context()
        self._install(dc.replace(ctx, allowlist=ctx.allowlist | self._programs))


def allow(*programs: Program) -> AllowRegistration:
    """Extend the current context's allowlist with additional programs.

    Parameters
    ----------
    programs:
        Programs to add to the allowlist.

    Returns
    -------
    AllowRegistration
        A handle that can be detached or used as a context manager.

    Example
    -------
    >>> with allow(LS):
    ...     assert current_context().is_allowed(LS)

    """
    return AllowRegistration(*programs)


class HookRegistration(_TokenRegistration):
    """Registration handle for hooks with detach and context-manager support.

    The token-restoration discipline is documented on
    :class:`_TokenRegistration`.
    """

    __slots__ = ("_hook", "_hook_type")

    def __init__(
        self,
        hook: BeforeHook | AfterHook | ExecHook,
        hook_type: typ.Literal["before", "after", "observe"],
    ) -> None:
        """Create a hook registration and add hook to current context."""
        super().__init__()
        self._hook = hook
        self._hook_type = hook_type
        ctx = current_context()
        if hook_type == "before":
            new_ctx = ctx.with_before_hook(typ.cast("BeforeHook", hook))
        elif hook_type == "after":
            new_ctx = ctx.with_after_hook(typ.cast("AfterHook", hook))
        else:
            new_ctx = ctx.with_observe_hook(typ.cast("ExecHook", hook))
        self._install(new_ctx)


def before(hook: BeforeHook) -> HookRegistration:
    """Register a before-execution hook in the current context.

    Parameters
    ----------
    hook:
        Callable invoked with the SafeCmd before execution.

    Returns
    -------
    HookRegistration
        A handle that can be detached or used as a context manager.

    Example
    -------
    >>> def log_cmd(cmd):
    ...     print(f"Running: {cmd.program}")
    >>> with before(log_cmd):
    ...     # Commands run here will trigger log_cmd
    ...     pass

    """
    return HookRegistration(hook, "before")


def after(hook: AfterHook) -> HookRegistration:
    """Register an after-execution hook in the current context.

    Parameters
    ----------
    hook:
        Callable invoked with the SafeCmd and CommandResult after execution.

    Returns
    -------
    HookRegistration
        A handle that can be detached or used as a context manager.

    Example
    -------
    >>> def log_result(cmd, result):
    ...     print(f"Finished: {cmd.program} -> {result.exit_code}")
    >>> with after(log_result):
    ...     # Commands run here will trigger log_result
    ...     pass

    """
    return HookRegistration(hook, "after")


class EnvRegistration(_TokenRegistration):
    """Registration handle for a scoped environment overlay.

    The overlay is layered on top of any overlay already present in the
    current context; nested registrations therefore behave as a stack. The
    token-restoration discipline is documented on
    :class:`_TokenRegistration`.

    The overlay itself is overlay-only. The live :func:`os.environ` is read at
    subprocess spawn time (see :func:`resolve_env`), so any updates to the
    process environment after the registration is created — for example via
    ``pytest``'s ``monkeypatch.setenv`` — remain visible to subprocesses
    spawned inside the scope. This is the behaviour the issue requires.
    """

    __slots__ = ("_overlay",)

    def __init__(self, overlay: cabc.Mapping[str, str]) -> None:
        """Layer ``overlay`` onto the current context's env overlay."""
        super().__init__()
        self._overlay = _coerce_env_overlay(overlay)
        self._install(current_context().with_env_overlay(self._overlay))

    @property
    def overlay(self) -> cabc.Mapping[str, str] | None:
        """Return the immutable overlay this registration applied."""
        return self._overlay


def env(
    *overlays: cabc.Mapping[str, str],
    **kwvars: str,
) -> EnvRegistration:
    """Overlay environment variables on top of the live :func:`os.environ`.

    Mirrors :func:`dict` in how arguments are combined: positional mappings
    are merged left-to-right and any keyword arguments win over them. Values
    are not snapshot against ``os.environ`` — the live process environment is
    read at subprocess spawn time so that variables set after Cuprum is
    imported (for example by ``monkeypatch.setenv``) remain visible.

    Parameters
    ----------
    overlays:
        Zero or more ``Mapping[str, str]`` instances supplying overlay
        entries. Useful when the variable name is not a valid Python
        identifier.
    kwvars:
        Keyword pairs naming environment variables. Identifier-safe variable
        names are typically expressed this way.

    Returns
    -------
    EnvRegistration
        A handle that can be detached or used as a context manager.

    Example
    -------
    >>> import os
    >>> os.environ["GIT_AUTHOR_NAME"] = "Cuprum"
    >>> with env(PATH="/usr/bin"):
    ...     # Subprocesses spawned here see PATH=/usr/bin overlaid on the
    ...     # *live* os.environ, including GIT_AUTHOR_NAME.
    ...     pass

    Notes
    -----
    Identical to other registration helpers, ``env`` is bound to the
    :class:`~contextvars.Context` in which it is created; detach in the same
    logical context (thread or task) to avoid ``ValueError`` from
    :meth:`~contextvars.ContextVar.reset`.
    """
    merged: dict[str, str] = {}
    for overlay in overlays:
        merged.update(overlay)
    if kwvars:
        merged.update(kwvars)
    return EnvRegistration(merged)


def observe(hook: ExecHook) -> HookRegistration:
    """Register a structured execution event hook in the current context.

    Parameters
    ----------
    hook:
        Callable invoked with :class:`~cuprum.events.ExecEvent` values as Cuprum
        executes commands and pipelines.

    Returns
    -------
    HookRegistration
        A handle that can be detached or used as a context manager.

    """
    return HookRegistration(hook, "observe")


__all__ = [
    "AllowRegistration",
    "EnvRegistration",
    "HookRegistration",
    "after",
    "allow",
    "before",
    "env",
    "observe",
    "scoped",
]
