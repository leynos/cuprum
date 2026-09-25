"""Execution-context scope configuration and domain errors.

Defines the :class:`ScopeConfig` dataclass used to describe scoped
allowlist and hook updates, plus :class:`ContextError` and
:class:`ForbiddenProgramError`, the errors raised by allowlist enforcement.
:class:`~cuprum.context.core.CuprumContext` consumes ``ScopeConfig`` when
narrowing; ``ContextVar`` plumbing lives in :mod:`cuprum.context.state` and
registration handles live in :mod:`cuprum.context.registration`.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import typing as typ

from cuprum.context._policy import _validate_timeout
from cuprum.context.env_overlay import _coerce_env_overlay

if typ.TYPE_CHECKING:
    from cuprum.events import ExecHook
    from cuprum.program import Program
    from cuprum.sh import CommandResult, SafeCmd

__all__ = [
    "AfterHook",
    "BeforeHook",
    "ContextError",
    "ForbiddenProgramError",
    "ScopeConfig",
]


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
