"""Hook-registration operations for the immutable execution context.

:class:`~cuprum.context.core.CuprumContext` exposes six mutators for its three
hook slots: a ``with_…_hook`` that registers one hook, and a
``without_…_hook`` that removes it by object identity. They live here rather
than in :mod:`cuprum.context.core` so that module stays inside the repository's
400-line-per-module ceiling without trimming the explanations the behaviour
needs; ADR-006 sets the same precedent for the rest of the package.

Before and observe hooks run FIFO and after hooks run LIFO, which is why
:meth:`_HookPolicy.with_after_hook` prepends while the other two append.
Removal is by identity rather than equality, so two distinct callables that
happen to compare equal are tracked separately.

The mixin holds no ``ContextVar`` state and never invokes a hook; it derives
value objects only.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.context._scope import AfterHook, BeforeHook
    from cuprum.events import ExecHook

__all__ = ["_HookPolicy"]


class _HookPolicy:
    """Adds hook-registration operations used by `CuprumContext`."""

    __slots__ = ()

    if typ.TYPE_CHECKING:
        # Supplied by the dataclass that mixes this in. Declared here so the
        # methods below type-check against the fields they read.
        before_hooks: tuple[BeforeHook, ...]
        after_hooks: tuple[AfterHook, ...]
        observe_hooks: tuple[ExecHook, ...]
        # ``dc.replace`` needs the concrete class to be a dataclass; the
        # declaration tells a type checker what the runtime already knows.
        __dataclass_fields__: typ.ClassVar[dict[str, dc.Field[typ.Any]]]

    def with_before_hook(self, hook: BeforeHook) -> typ.Self:
        """Return a context with an additional before hook.

        Parameters
        ----------
        hook : BeforeHook
            The before-execution hook to append.

        Returns
        -------
        CuprumContext
            A new context with the before hook appended.
        """
        return dc.replace(self, before_hooks=(*self.before_hooks, hook))

    def without_before_hook(self, hook: BeforeHook) -> typ.Self:
        """Return a context with the specified before hook removed.

        Parameters
        ----------
        hook : BeforeHook
            The before-execution hook to remove.

        Returns
        -------
        CuprumContext
            A new context without the given before hook.
        """
        new_hooks = tuple(h for h in self.before_hooks if h is not hook)
        return dc.replace(self, before_hooks=new_hooks)

    def with_after_hook(self, hook: AfterHook) -> typ.Self:
        """Return a context with an additional after hook (prepended for LIFO).

        Parameters
        ----------
        hook : AfterHook
            The after-execution hook to prepend.

        Returns
        -------
        CuprumContext
            A new context with the after hook prepended.
        """
        return dc.replace(self, after_hooks=(hook, *self.after_hooks))

    def without_after_hook(self, hook: AfterHook) -> typ.Self:
        """Return a context with the specified after hook removed.

        Parameters
        ----------
        hook : AfterHook
            The after-execution hook to remove.

        Returns
        -------
        CuprumContext
            A new context without the given after hook.
        """
        new_hooks = tuple(h for h in self.after_hooks if h is not hook)
        return dc.replace(self, after_hooks=new_hooks)

    def with_observe_hook(self, hook: ExecHook) -> typ.Self:
        """Return a context with an additional observe hook.

        Parameters
        ----------
        hook : ExecHook
            The structured-event observe hook to append.

        Returns
        -------
        CuprumContext
            A new context with the observe hook appended.
        """
        return dc.replace(self, observe_hooks=(*self.observe_hooks, hook))

    def without_observe_hook(self, hook: ExecHook) -> typ.Self:
        """Return a context with the specified observe hook removed.

        Parameters
        ----------
        hook : ExecHook
            The structured-event observe hook to remove.

        Returns
        -------
        CuprumContext
            A new context without the given observe hook.
        """
        new_hooks = tuple(h for h in self.observe_hooks if h is not hook)
        return dc.replace(self, observe_hooks=new_hooks)
