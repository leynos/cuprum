"""Scoped-context managers and registration handles.

Provides ``scoped`` plus the user-facing registration factories (``allow``,
``before``, ``after``, ``observe``, ``env``, ``bind_executable``). All
registration handles derive from the canonical :class:`_TokenRegistration`
base, which owns the ``ContextVar`` token-restoration discipline.
"""

from __future__ import annotations

import typing as typ

from cuprum.context._env_registration import EnvRegistration, env
from cuprum.context._registration_base import _TokenRegistration
from cuprum.context.scoped import scoped
from cuprum.context.state import current_context
from cuprum.executable_binding import (
    ExecutableBinding,
    ExecutableResolver,
    executable_binding,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.context.core import (
        AfterHook,
        BeforeHook,
        CuprumContext,
    )
    from cuprum.events import ExecHook
    from cuprum.program import Program


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
        self._install(ctx.with_allowlist(ctx.allowlist | self._programs))


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


def _context_with_hook(
    ctx: CuprumContext,
    hook: BeforeHook | AfterHook | ExecHook,
    hook_type: typ.Literal["before", "after", "observe"],
) -> CuprumContext:
    """Derive a context carrying ``hook`` registered under ``hook_type``."""
    # The tag and callable signature are correlated by the public factories.
    # Narrow here because Python cannot express that dependent union directly.
    match hook_type:
        case "before":
            return ctx.with_before_hook(typ.cast("BeforeHook", hook))
        case "after":
            return ctx.with_after_hook(typ.cast("AfterHook", hook))
        case "observe":
            return ctx.with_observe_hook(typ.cast("ExecHook", hook))
        case _:
            msg = f"Unsupported hook type: {hook_type}"
            raise ValueError(msg)


class HookRegistration(_TokenRegistration):
    """Registration handle for hooks with detach and context-manager support.

    The token-restoration discipline is documented on
    :class:`_TokenRegistration`.

    Prefer the :func:`before`, :func:`after`, and :func:`observe` factories:
    each pairs one hook shape with its slot, so a mismatched hook is rejected
    at the call site. Constructing the handle directly pairs the hook and the
    tag by hand, and only the tag itself is validated — at runtime.
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
        self._install(_context_with_hook(current_context(), hook, hook_type))


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


class ExecutableBindingRegistration(_TokenRegistration):
    """Registration handle for a scoped executable binding.

    The binding is layered onto any bindings already present in the current
    context, so nested registrations behave as a stack: the innermost binding
    for a program is the one an execution resolves. The token-restoration
    discipline is documented on :class:`_TokenRegistration`.

    A binding supplies the executable a permitted program runs. It is not a
    permission: the allowlist continues to decide which logical programs may
    run at all, and resolution happens only after that decision has been made.
    """

    __slots__ = ("_binding", "_program")

    def __init__(
        self,
        program: Program,
        binding: ExecutableBinding,
    ) -> None:
        """Register ``binding`` for ``program`` in the current context.

        Parameters
        ----------
        program:
            The logical program to bind.
        binding:
            The executable the program should run within the scope.
        """
        super().__init__()
        self._program = program
        self._binding = binding
        self._install(current_context().with_executable_binding(program, binding))

    @property
    def program(self) -> Program:
        """The logical program this registration bound."""
        return self._program

    @property
    def binding(self) -> ExecutableBinding:
        """The binding this registration applied."""
        return self._binding


def bind_executable(
    program: Program,
    path_or_resolver: str | Path | ExecutableResolver,
    *,
    allow_relative: bool = False,
) -> ExecutableBindingRegistration:
    """Bind a logical program to an executable for the enclosing scope.

    The binding applies only inside the scope; leaving it restores the
    bindings that were in effect before. A nested registration for the same
    program overrides the outer one for the inner scope alone, and bindings
    for distinct programs compose.

    Parameters
    ----------
    program:
        The logical program to bind.
    path_or_resolver:
        The executable path, or a zero-argument callable returning one. A
        resolver is evaluated once per execution, at spawn time.
    allow_relative:
        When True, a relative path is permitted and anchored at the
        execution's working directory. Ignored for a resolver.

    Returns
    -------
    ExecutableBindingRegistration
        A handle that can be detached or used as a context manager.

    Raises
    ------
    InvalidExecutableBindingError
        A supplied path failed validation. The error names the logical
        program, quotes the path, and carries the classified reason.

    Examples
    --------
    >>> with bind_executable(ECHO, "/opt/tools/echo"):
    ...     # Commands for ECHO run /opt/tools/echo inside this scope.
    ...     pass

    """  # ruff: ignore[docstring-extraneous-exception] - InvalidExecutableBindingError propagates from executable_binding.
    return ExecutableBindingRegistration(
        program,
        executable_binding(program, path_or_resolver, allow_relative=allow_relative),
    )


__all__ = [
    "AllowRegistration",
    "EnvRegistration",
    "ExecutableBindingRegistration",
    "HookRegistration",
    "after",
    "allow",
    "before",
    "bind_executable",
    "env",
    "observe",
    "scoped",
]
