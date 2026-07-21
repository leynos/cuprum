"""Core execution-context domain types.

Defines the immutable :class:`CuprumContext` and :class:`ScopeConfig`
dataclasses, the :class:`ForbiddenProgramError` raised by allowlist
enforcement, and the hook type aliases. ``ContextVar`` plumbing lives in
:mod:`cuprum.context.state`; registration handles live in
:mod:`cuprum.context.registration`.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import logging
import math
import typing as typ

from cuprum.context.env_overlay import _coerce_env_overlay, merge_env_overlays

if typ.TYPE_CHECKING:
    from cuprum.events import ExecHook
    from cuprum.program import Program
    from cuprum.sh import CommandResult, SafeCmd

_logger = logging.getLogger("cuprum.context")


type BeforeHook = cabc.Callable[[SafeCmd], None]
type AfterHook = cabc.Callable[[SafeCmd, CommandResult], None]


class ContextError(Exception):
    """Base class for execution-context domain errors."""


class ForbiddenProgramError(ContextError, PermissionError):
    """Raised when attempting to run a program not in the current allowlist.

    Attributes
    ----------
    program : Program
        The program that was denied by the context allowlist.
    restricted_state : bool
        Whether the context allowlist was in a restricted state when the
        program was denied.
    """

    def __init__(self, program: Program, *, restricted_state: bool) -> None:
        """Describe the denied program and allowlist restriction state."""
        self.program = program
        self.restricted_state = restricted_state
        msg = f"Program '{program}' is not allowed in the current context"
        super().__init__(msg)


def _validate_timeout(timeout: float | None, class_name: str) -> float | None:
    """Validate a timeout is finite and non-negative, coercing it to float."""
    if timeout is None:
        return None
    timeout_float = float(timeout)
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


def _merge_before_hooks(
    parent: tuple[BeforeHook, ...],
    config: tuple[BeforeHook, ...],
) -> tuple[BeforeHook, ...]:
    """Append scoped before hooks after parent hooks for FIFO execution."""
    return parent + config


def _merge_after_hooks(
    parent: tuple[AfterHook, ...],
    config: tuple[AfterHook, ...],
) -> tuple[AfterHook, ...]:
    """Prepend scoped after hooks before parent hooks for LIFO execution."""
    return config + parent


def _merge_observe_hooks(
    parent: tuple[ExecHook, ...],
    config: tuple[ExecHook, ...],
) -> tuple[ExecHook, ...]:
    """Append scoped observe hooks after parent hooks for FIFO execution."""
    return parent + config


def _resolve_narrowed_timeout(
    parent: float | None, config: float | None
) -> float | None:
    """Return the timeout inherited or overridden by a narrowed context."""
    if config is None:
        return parent
    return config


@dc.dataclass(frozen=True, slots=True)
class ScopeConfig:
    """Configuration object for scoped execution context updates.

    Attributes
    ----------
    allowlist:
        Optional allowlist for the scope. When ``None``, inherit the current
        allowlist.
    before_hooks:
        Hooks invoked before command execution (FIFO order).
    after_hooks:
        Hooks invoked after command execution (LIFO order).
    observe_hooks:
        Hooks invoked for structured execution events.
    timeout:
        Optional default timeout in seconds for calls within the scope.
    env_overlay:
        Optional immutable environment overlay layered over the live
        ``os.environ`` at subprocess spawn time. When ``None``, no overlay
        is applied within the scope.

    """

    allowlist: frozenset[Program] | None = None
    before_hooks: tuple[BeforeHook, ...] = ()
    after_hooks: tuple[AfterHook, ...] = ()
    observe_hooks: tuple[ExecHook, ...] = ()
    timeout: float | None = None
    env_overlay: cabc.Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        """Validate and coerce timeout after initialization."""
        validated = _validate_timeout(self.timeout, "ScopeConfig")
        # Use object.__setattr__ because the dataclass is frozen
        object.__setattr__(self, "timeout", validated)
        object.__setattr__(
            self,
            "env_overlay",
            _coerce_env_overlay(self.env_overlay),
        )


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

    """

    allowlist: frozenset[Program] = dc.field(default_factory=frozenset)
    before_hooks: tuple[BeforeHook, ...] = ()
    after_hooks: tuple[AfterHook, ...] = ()
    observe_hooks: tuple[ExecHook, ...] = ()
    timeout: float | None = None
    env_overlay: cabc.Mapping[str, str] | None = None
    _allowlist_is_restricted: bool = False

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
        return CuprumContext(
            allowlist=_narrow_allowlist(
                self.allowlist,
                config.allowlist,
                parent_is_restricted=self._allowlist_is_restricted,
            ),
            before_hooks=_merge_before_hooks(self.before_hooks, config.before_hooks),
            after_hooks=_merge_after_hooks(self.after_hooks, config.after_hooks),
            observe_hooks=_merge_observe_hooks(
                self.observe_hooks, config.observe_hooks
            ),
            timeout=_resolve_narrowed_timeout(self.timeout, config.timeout),
            env_overlay=merge_env_overlays(self.env_overlay, config.env_overlay),
            _allowlist_is_restricted=is_restricted,
        )

    def with_allowlist(self, allowlist: frozenset[Program]) -> CuprumContext:
        """Return a context with the given allowlist replacing the current one.

        Unlike narrow(), this sets the allowlist directly without intersection.
        Use with care; prefer narrow() for enforcing safety invariants.
        """
        return dc.replace(
            self,
            allowlist=allowlist,
            _allowlist_is_restricted=self._allowlist_is_restricted
            or bool(self.allowlist)
            or bool(allowlist),
        )

    def with_before_hook(self, hook: BeforeHook) -> CuprumContext:
        """Return a context with an additional before hook."""
        return dc.replace(self, before_hooks=(*self.before_hooks, hook))

    def without_before_hook(self, hook: BeforeHook) -> CuprumContext:
        """Return a context with the specified before hook removed."""
        new_hooks = tuple(h for h in self.before_hooks if h is not hook)
        return dc.replace(self, before_hooks=new_hooks)

    def with_after_hook(self, hook: AfterHook) -> CuprumContext:
        """Return a context with an additional after hook (prepended for LIFO)."""
        return dc.replace(self, after_hooks=(hook, *self.after_hooks))

    def without_after_hook(self, hook: AfterHook) -> CuprumContext:
        """Return a context with the specified after hook removed."""
        new_hooks = tuple(h for h in self.after_hooks if h is not hook)
        return dc.replace(self, after_hooks=new_hooks)

    def with_observe_hook(self, hook: ExecHook) -> CuprumContext:
        """Return a context with an additional observe hook."""
        return dc.replace(self, observe_hooks=(*self.observe_hooks, hook))

    def without_observe_hook(self, hook: ExecHook) -> CuprumContext:
        """Return a context with the specified observe hook removed."""
        new_hooks = tuple(h for h in self.observe_hooks if h is not hook)
        return dc.replace(self, observe_hooks=new_hooks)

    def with_program(self, program: Program) -> CuprumContext:
        """Return a context with the program added to the allowlist."""
        return self.with_allowlist(self.allowlist | {program})

    def without_program(self, program: Program) -> CuprumContext:
        """Return a context with the program removed from the allowlist."""
        return self.with_allowlist(self.allowlist - {program})

    def with_env_overlay(
        self,
        overlay: cabc.Mapping[str, str] | None,
    ) -> CuprumContext:
        """Return a context whose env overlay is layered with ``overlay``.

        Values in ``overlay`` win over earlier overlay entries, but no value
        ever displaces the live :func:`os.environ`; the live process
        environment is read at subprocess spawn time. Passing ``None`` returns
        an unchanged copy.
        """
        return dc.replace(
            self,
            env_overlay=merge_env_overlays(self.env_overlay, overlay),
        )


__all__ = [
    "AfterHook",
    "BeforeHook",
    "ContextError",
    "CuprumContext",
    "ForbiddenProgramError",
    "ScopeConfig",
]
