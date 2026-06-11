"""ContextVar plumbing for the active execution context.

Owns the process-wide ``ContextVar`` holding the current
:class:`~cuprum.context.core.CuprumContext` and the internal set/reset
helpers used by the registration handles.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from cuprum.context.core import CuprumContext

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


__all__ = [
    "current_context",
    "get_context",
]
