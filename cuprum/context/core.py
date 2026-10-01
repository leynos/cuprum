"""Core execution-context domain types.

Defines the immutable :class:`CuprumContext` dataclass. The
:class:`ScopeConfig` dataclass it consumes when narrowing, the
:class:`ContextError` and :class:`ForbiddenProgramError` errors raised by
allowlist enforcement, and the hook type aliases live in
:mod:`cuprum.context._scope` and are re-exported here for the public API.
``ContextVar`` plumbing lives in :mod:`cuprum.context.state`; registration
handles live in :mod:`cuprum.context.registration`.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ

from cuprum.context._policy import (
    _is_narrowed_allowlist_restricted,
    _merge_hooks,
    _narrow_allowlist,
    _resolve_env_policy,
    _resolve_narrowed_timeout,
    _validate_timeout,
)
from cuprum.context._scope import (
    AfterHook,
    BeforeHook,
    ContextError,
    ForbiddenProgramError,
    ScopeConfig,
)
from cuprum.context.env_overlay import EnvMode, EnvOverlay, _coerce_env_overlay

if typ.TYPE_CHECKING:
    from cuprum.events import ExecHook
    from cuprum.program import Program

_logger = logging.getLogger("cuprum.context")


@dc.dataclass(frozen=True, slots=True)
class CuprumContext:
    """Immutable execution context holding allowlist and hooks.

    Attributes
    ----------
    allowlist:
        Frozenset of programs permitted in this context.
    before_hooks:
        Tuple of hooks invoked before command execution (FIFO order).
    after_hooks:
        Tuple of hooks invoked after command execution (LIFO order).
    observe_hooks:
        Tuple of hooks invoked for structured execution events (FIFO order).
    timeout:
        Optional default timeout in seconds applied when a call does not supply
        an explicit timeout.
    env_overlay:
        Optional immutable environment overlay layered over the live
        ``os.environ`` when command environments are resolved. When ``None``,
        no overlay is active on this context.
    _allowlist_is_restricted:
        Internal marker distinguishing the permissive empty default allowlist
        from an empty allowlist produced by narrowing a restricted scope.
    env_mode:
        Policy used to render the composed environment for child processes.

    """

    allowlist: frozenset[Program] = dc.field(default_factory=frozenset)
    before_hooks: tuple[BeforeHook, ...] = ()
    after_hooks: tuple[AfterHook, ...] = ()
    observe_hooks: tuple[ExecHook, ...] = ()
    timeout: float | None = None
    env_overlay: EnvOverlay | None = None
    _allowlist_is_restricted: bool = False
    env_mode: EnvMode = EnvMode.OVERLAY

    def __post_init__(self) -> None:
        """Validate and coerce timeout after initialization."""
        validated = _validate_timeout(self.timeout, "CuprumContext")
        # Use object.__setattr__ because the dataclass is frozen
        object.__setattr__(self, "timeout", validated)
        object.__setattr__(
            self,
            "env_overlay",
            _coerce_env_overlay(self.env_overlay),
        )

    def is_allowed(self, program: Program) -> bool:
        """Return True when the program is in the allowlist.

        This method only checks membership and returns False for empty
        allowlists. Use check_allowed() for enforcement; it applies the
        two-mode empty-allowlist policy, permitting empty unrestricted
        contexts and denying empty restricted contexts.

        Parameters
        ----------
        program : Program
            The program to check against the current allowlist.

        Returns
        -------
        bool
            ``True`` when the program is a member of the allowlist.
        """
        return program in self.allowlist

    def check_allowed(self, program: Program) -> None:
        """Raise ForbiddenProgramError if program is not allowed.

        When the allowlist is empty and unrestricted, all programs are
        permitted (permissive default). When the allowlist is empty and
        restricted, all programs are denied.

        A warning log with operation and restricted_state fields is emitted
        before raising ForbiddenProgramError.

        Raises
        ------
        ForbiddenProgramError
            If the program is not permitted by the context allowlist.
        """
        if not self.allowlist and not self._allowlist_is_restricted:
            return  # Empty allowlist permits all programs
        if not self.is_allowed(program):
            _logger.warning(
                "Program %s denied by context allowlist restricted_state=%s",
                program,
                self._allowlist_is_restricted,
                extra={
                    "operation": program,
                    "restricted_state": self._allowlist_is_restricted,
                },
            )
            raise ForbiddenProgramError(
                program,
                restricted_state=self._allowlist_is_restricted,
            )

    def narrow(self, config: ScopeConfig) -> CuprumContext:
        """Create a derived context with narrowed allowlist and extended hooks.

        Parameters
        ----------
        config:
            Scope configuration describing allowlist and hook updates.

        Returns
        -------
        CuprumContext
            A new context with narrowed permissions and extended hooks.

        Notes
        -----
        When the parent has an empty *unrestricted* allowlist, the provided
        allowlist is used directly to establish a base scope. When the parent
        is restricted and empty, narrowing keeps it empty. When the parent has
        programs, the new allowlist is intersected to enforce narrowing (can
        only remove, not add programs).

        """
        is_restricted = _is_narrowed_allowlist_restricted(
            config.allowlist,
            parent_is_restricted=self._allowlist_is_restricted,
        )
        env_overlay, env_mode = _resolve_env_policy(
            self.env_overlay,
            self.env_mode,
            config.env_overlay,
            config.env_mode,
        )
        return CuprumContext(
            allowlist=_narrow_allowlist(
                self.allowlist,
                config.allowlist,
                parent_is_restricted=self._allowlist_is_restricted,
            ),
            before_hooks=_merge_hooks(
                self.before_hooks, config.before_hooks, scoped_first=False
            ),
            after_hooks=_merge_hooks(
                self.after_hooks, config.after_hooks, scoped_first=True
            ),
            observe_hooks=_merge_hooks(
                self.observe_hooks, config.observe_hooks, scoped_first=False
            ),
            timeout=_resolve_narrowed_timeout(self.timeout, config.timeout),
            env_overlay=env_overlay,
            env_mode=env_mode,
            _allowlist_is_restricted=is_restricted,
        )

    def with_allowlist(self, allowlist: frozenset[Program]) -> CuprumContext:
        """Return a context with the given allowlist replacing the current one.

        Unlike narrow(), this sets the allowlist directly without intersection.
        Use with care; prefer narrow() for enforcing safety invariants.

        Parameters
        ----------
        allowlist : frozenset[Program]
            The allowlist to install in place of the current one.

        Returns
        -------
        CuprumContext
            A new context with the supplied allowlist.
        """
        return dc.replace(
            self,
            allowlist=allowlist,
            _allowlist_is_restricted=self._allowlist_is_restricted
            or bool(self.allowlist)
            or bool(allowlist),
        )

    def with_before_hook(self, hook: BeforeHook) -> CuprumContext:
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

    def without_before_hook(self, hook: BeforeHook) -> CuprumContext:
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

    def with_after_hook(self, hook: AfterHook) -> CuprumContext:
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

    def without_after_hook(self, hook: AfterHook) -> CuprumContext:
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

    def with_observe_hook(self, hook: ExecHook) -> CuprumContext:
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

    def without_observe_hook(self, hook: ExecHook) -> CuprumContext:
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

    def with_program(self, program: Program) -> CuprumContext:
        """Return a context with the program added to the allowlist.

        Parameters
        ----------
        program : Program
            The program to add to the allowlist.

        Returns
        -------
        CuprumContext
            A new context whose allowlist includes the program.
        """
        return self.with_allowlist(self.allowlist | {program})

    def without_program(self, program: Program) -> CuprumContext:
        """Return a context with the program removed from the allowlist.

        Parameters
        ----------
        program : Program
            The program to remove from the allowlist.

        Returns
        -------
        CuprumContext
            A new context whose allowlist excludes the program.
        """
        return self.with_allowlist(self.allowlist - {program})

    def with_env_overlay(
        self,
        overlay: EnvOverlay | None,
        mode: EnvMode = EnvMode.OVERLAY,
    ) -> CuprumContext:
        """Return a context whose env overlay is layered with ``overlay``.

        Values in ``overlay`` win over earlier overlay entries. A replacement
        policy discards earlier layers, while every other policy reads the live
        :func:`os.environ` at subprocess spawn time. In non-replacement modes,
        passing ``None`` leaves the effective overlay unchanged; in replacement
        mode, it creates an empty replacement boundary.

        Parameters
        ----------
        overlay : collections.abc.Mapping[str, str | UnsetType] | None
            Environment variables to layer over the current overlay. ``None``
            leaves the current overlay unchanged.
        mode : EnvMode
            Policy contributed by this child layer.

        Returns
        -------
        CuprumContext
            A new context with the merged environment overlay.
        """
        env_overlay, env_mode = _resolve_env_policy(
            self.env_overlay,
            self.env_mode,
            overlay,
            mode,
        )
        return dc.replace(self, env_overlay=env_overlay, env_mode=env_mode)


__all__ = [
    "AfterHook",
    "BeforeHook",
    "ContextError",
    "CuprumContext",
    "ForbiddenProgramError",
    "ScopeConfig",
]
