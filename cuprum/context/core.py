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
import typing as typ

from cuprum.context.env_overlay import _coerce_env_overlay, merge_env_overlays

if typ.TYPE_CHECKING:
    from cuprum.events import ExecHook
    from cuprum.program import Program
    from cuprum.sh import CommandResult, SafeCmd


type BeforeHook = cabc.Callable[[SafeCmd], None]
type AfterHook = cabc.Callable[[SafeCmd, CommandResult], None]


class ForbiddenProgramError(PermissionError):
    """Raised when attempting to run a program not in the current allowlist."""


def _validate_timeout(timeout: float | None, class_name: str) -> float | None:
    """Validate and coerce timeout value.

    Parameters
    ----------
    timeout:
        The timeout value to validate. May be None, float, or int.
    class_name:
        Name of the class for error messages.

    Returns
    -------
    float | None
        The validated timeout as a float, or None.

    Raises
    ------
    ValueError
        When timeout is negative.

    """
    if timeout is None:
        return None
    timeout_float = float(timeout)
    if timeout_float < 0:
        msg = f"{class_name} timeout must be non-negative, got {timeout_float}"
        raise ValueError(msg)
    return timeout_float


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

    """

    allowlist: frozenset[Program] = dc.field(default_factory=frozenset)
    before_hooks: tuple[BeforeHook, ...] = ()
    after_hooks: tuple[AfterHook, ...] = ()
    observe_hooks: tuple[ExecHook, ...] = ()
    timeout: float | None = None
    env_overlay: cabc.Mapping[str, str] | None = None

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

        Note: An empty allowlist returns False for all programs, but
        check_allowed() treats an empty allowlist as permissive.
        Use check_allowed() for enforcement with permissive defaults.
        """
        return program in self.allowlist

    def check_allowed(self, program: Program) -> None:
        """Raise ForbiddenProgramError if program is not allowed.

        When the allowlist is empty, all programs are permitted (permissive
        default). This allows gradual adoption: code can run without explicit
        context setup, and scoped(ScopeConfig()) can later establish restrictions.
        """
        if not self.allowlist:
            return  # Empty allowlist permits all programs
        if not self.is_allowed(program):
            msg = f"Program '{program}' is not allowed in the current context"
            raise ForbiddenProgramError(msg)

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
        When the parent has an empty allowlist, the provided allowlist is used
        directly to establish a base scope. When the parent has programs, the
        new allowlist is intersected to enforce narrowing (can only remove, not
        add programs). This ensures safety while allowing initial setup.

        """
        if config.allowlist is None:
            new_allowlist = self.allowlist
        elif self.allowlist:
            # Parent has programs: intersect to narrow
            new_allowlist = self.allowlist & config.allowlist
        else:
            # Parent is empty: use provided allowlist as new base
            new_allowlist = config.allowlist

        new_before = self.before_hooks + config.before_hooks
        # After hooks run inner-to-outer, so prepend new hooks
        new_after = config.after_hooks + self.after_hooks
        new_observe = self.observe_hooks + config.observe_hooks
        new_timeout = self.timeout if config.timeout is None else config.timeout
        new_env_overlay = merge_env_overlays(self.env_overlay, config.env_overlay)

        return CuprumContext(
            allowlist=new_allowlist,
            before_hooks=new_before,
            after_hooks=new_after,
            observe_hooks=new_observe,
            timeout=new_timeout,
            env_overlay=new_env_overlay,
        )

    def with_allowlist(self, allowlist: frozenset[Program]) -> CuprumContext:
        """Return a context with the given allowlist replacing the current one.

        Unlike narrow(), this sets the allowlist directly without intersection.
        Use with care; prefer narrow() for enforcing safety invariants.
        """
        return dc.replace(self, allowlist=allowlist)

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
        return dc.replace(self, allowlist=self.allowlist | {program})

    def without_program(self, program: Program) -> CuprumContext:
        """Return a context with the program removed from the allowlist."""
        return dc.replace(self, allowlist=self.allowlist - {program})

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
    "CuprumContext",
    "ForbiddenProgramError",
    "ScopeConfig",
]
