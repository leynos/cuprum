"""Execution context with scoped allowlists and hooks.

CuprumContext provides a ContextVar-backed execution context that scopes
allowlists and hooks for command execution. Contexts support narrowing
(restricting the allowlist) and hook registration with deterministic ordering.

Example:
>>> from cuprum.context import scoped, before, current_context
>>> from cuprum.catalogue import ECHO
>>> def log_hook(cmd):
...     print(f"Running: {cmd}")
>>> with scoped(allowlist=frozenset([ECHO])):
...     with before(log_hook):
...         ctx = current_context()
...         ctx.is_allowed(ECHO)
True

"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import typing as typ
from contextvars import ContextVar, Token

from cuprum.events import ExecHook

if typ.TYPE_CHECKING:
    from cuprum.program import Program
    from cuprum.sh import CommandResult, SafeCmd


type BeforeHook = cabc.Callable[[SafeCmd], None]
type AfterHook = cabc.Callable[[SafeCmd, CommandResult], None]


class ForbiddenProgramError(PermissionError):
    """Raised when attempting to run a program not in the current allowlist."""


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

    """

    allowlist: frozenset[Program] = dc.field(default_factory=frozenset)
    before_hooks: tuple[BeforeHook, ...] = ()
    after_hooks: tuple[AfterHook, ...] = ()
    observe_hooks: tuple[ExecHook, ...] = ()

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
        context setup, and scoped() can later establish restrictions.
        """
        if not self.allowlist:
            return  # Empty allowlist permits all programs
        if not self.is_allowed(program):
            msg = f"Program '{program}' is not allowed in the current context"
            raise ForbiddenProgramError(msg)

    def narrow(
        self,
        *,
        allowlist: frozenset[Program] | None = None,
        before_hooks: tuple[BeforeHook, ...] = (),
        after_hooks: tuple[AfterHook, ...] = (),
        observe_hooks: tuple[ExecHook, ...] = (),
    ) -> CuprumContext:
        """Create a derived context with narrowed allowlist and extended hooks.

        Parameters
        ----------
        allowlist:
            New allowlist; intersected with parent if parent is non-empty,
            otherwise used directly. None keeps the parent list unchanged.
        before_hooks:
            Additional before hooks appended after parent hooks.
        after_hooks:
            Additional after hooks prepended before parent hooks (LIFO).
        observe_hooks:
            Additional observe hooks appended after parent hooks (FIFO).

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
        if allowlist is None:
            new_allowlist = self.allowlist
        elif self.allowlist:
            # Parent has programs: intersect to narrow
            new_allowlist = self.allowlist & allowlist
        else:
            # Parent is empty: use provided allowlist as new base
            new_allowlist = allowlist

        new_before = self.before_hooks + before_hooks
        # After hooks run inner-to-outer, so prepend new hooks
        new_after = after_hooks + self.after_hooks
        new_observe = self.observe_hooks + observe_hooks

        return CuprumContext(
            allowlist=new_allowlist,
            before_hooks=new_before,
            after_hooks=new_after,
            observe_hooks=new_observe,
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


# Global ContextVar for the current execution context.
# Default is the singleton _DEFAULT_CONTEXT which is immutable (frozen dataclass).
_DEFAULT_CONTEXT = CuprumContext()
_current_context: ContextVar[CuprumContext] = ContextVar(
    "cuprum_context",
    default=_DEFAULT_CONTEXT,
)


def current_context() -> CuprumContext:
    """Return the current execution context."""
    return _current_context.get()


def get_context() -> CuprumContext:
    """Alias for current_context()."""
    return current_context()


def _set_context(ctx: CuprumContext) -> Token[CuprumContext]:
    """Set the current context and return a token for restoration."""
    return _current_context.set(ctx)


def _reset_context(token: Token[CuprumContext]) -> None:
    """Restore the context to its previous state using the token."""
    _current_context.reset(token)


class _ScopedContext:
    """Context manager for entering a scoped execution context."""

    __slots__ = ("_ctx", "_token")

    def __init__(
        self,
        *,
        allowlist: frozenset[Program] | None = None,
        before_hooks: tuple[BeforeHook, ...] = (),
        after_hooks: tuple[AfterHook, ...] = (),
        observe_hooks: tuple[ExecHook, ...] = (),
    ) -> None:
        parent = current_context()
        self._ctx = parent.narrow(
            allowlist=allowlist,
            before_hooks=before_hooks,
            after_hooks=after_hooks,
            observe_hooks=observe_hooks,
        )
        self._token: Token[CuprumContext] | None = None

    def __enter__(self) -> CuprumContext:
        self._token = _set_context(self._ctx)
        return self._ctx

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        if self._token is not None:
            _reset_context(self._token)


def scoped(
    *,
    allowlist: frozenset[Program] | None = None,
    before_hooks: tuple[BeforeHook, ...] = (),
    after_hooks: tuple[AfterHook, ...] = (),
    observe_hooks: tuple[ExecHook, ...] = (),
) -> _ScopedContext:
    """Create a scoped context manager for narrowed execution.

    Parameters
    ----------
    allowlist:
        Programs to allow (intersected with parent allowlist).
    before_hooks:
        Hooks to run before command execution.
    after_hooks:
        Hooks to run after command execution.
    observe_hooks:
        Hooks to run for structured execution events.

    Returns
    -------
    _ScopedContext
        A context manager that narrows the current context.

    Example
    -------
    >>> with scoped(allowlist=frozenset([ECHO])) as ctx:
    ...     assert ctx.is_allowed(ECHO)

    """
    return _ScopedContext(
        allowlist=allowlist,
        before_hooks=before_hooks,
        after_hooks=after_hooks,
        observe_hooks=observe_hooks,
    )


class AllowRegistration:
    """Registration handle for dynamic allowlist extension.

    Supports detach() and context manager usage for scoped allowing.

    Token-based Restoration
    -----------------------
    The registration captures a token at creation time. When detach() is called,
    the original context is restored via the token, ensuring no context pollution
    even when used outside scoped() blocks. This means detach() restores the
    exact context that existed when the registration was created, regardless of
    subsequent context modifications. If multiple registrations are created and
    detached in non-LIFO order, earlier tokens may restore states that remove
    programs added by other registrations.

    Detach in the same logical Context (thread or Task) in which the
    registration was created. Resetting a ContextVar with a token from a
    different Context raises ValueError and would break this guarantee.
    """

    __slots__ = ("_detached", "_programs", "_token")

    def __init__(self, *programs: Program) -> None:
        """Create an allowlist registration and add programs to current context."""
        self._programs = frozenset(programs)
        self._detached = False
        # Add programs to current context and capture token for restoration
        ctx = current_context()
        new_ctx = dc.replace(ctx, allowlist=ctx.allowlist | self._programs)
        self._token: Token[CuprumContext] | None = _set_context(new_ctx)

    def detach(self) -> None:
        """Restore the original context via the captured token."""
        if self._detached:
            return
        self._detached = True
        if self._token is not None:
            _reset_context(self._token)
            self._token = None

    def __enter__(self) -> AllowRegistration:
        """Enter context manager; programs are already registered."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit context manager; detach registered programs."""
        self.detach()


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


class HookRegistration:
    """Registration handle for hooks with detach() and context manager support.

    Token-based Restoration
    -----------------------
    The registration captures a token at creation time. When detach() is called,
    the original context is restored via the token, ensuring no context pollution
    even when used outside scoped() blocks. This means detach() restores the
    exact context that existed when the registration was created, regardless of
    subsequent context modifications.

    As with AllowRegistration, detach the hook only from the Context where
    it was registered; using the token in a different Context will raise
    ValueError in the standard library.
    """

    __slots__ = ("_detached", "_hook", "_hook_type", "_token")

    def __init__(
        self,
        hook: BeforeHook | AfterHook | ExecHook,
        hook_type: typ.Literal["before", "after", "observe"],
    ) -> None:
        """Create a hook registration and add hook to current context."""
        self._hook = hook
        self._hook_type = hook_type
        self._detached = False
        # Add hook to current context and capture token for restoration
        ctx = current_context()
        if hook_type == "before":
            new_ctx = ctx.with_before_hook(typ.cast("BeforeHook", hook))
        elif hook_type == "after":
            new_ctx = ctx.with_after_hook(typ.cast("AfterHook", hook))
        else:
            new_ctx = ctx.with_observe_hook(typ.cast("ExecHook", hook))
        self._token: Token[CuprumContext] | None = _set_context(new_ctx)

    def detach(self) -> None:
        """Restore the original context via the captured token."""
        if self._detached:
            return
        self._detached = True
        if self._token is not None:
            _reset_context(self._token)
            self._token = None

    def __enter__(self) -> HookRegistration:
        """Enter context manager; hook is already registered."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit context manager; detach registered hook."""
        self.detach()


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
    "AfterHook",
    "AllowRegistration",
    "BeforeHook",
    "CuprumContext",
    "ExecHook",
    "ForbiddenProgramError",
    "HookRegistration",
    "after",
    "allow",
    "before",
    "current_context",
    "get_context",
    "observe",
    "scoped",
]
