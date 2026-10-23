"""ContextVar plumbing for active execution and scoped-entry state.

Owns the process-wide ``ContextVar`` holding the current
:class:`~cuprum.context.core.CuprumContext`, plus the context-local stack of
scoped entries and the helpers that activate and restore this state.
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
    """Return the current execution context.

    Returns
    -------
    CuprumContext
        The context bound to the current execution scope.
    """
    return _current_context.get()


get_context = current_context


def _set_context(ctx: CuprumContext) -> Token[CuprumContext]:
    """Set the current context and return a token for restoration."""
    return _current_context.set(ctx)


def _reset_context(token: Token[CuprumContext]) -> None:
    """Restore the context to its previous state using the token."""
    _current_context.reset(token)


type _ScopedEntry = tuple[object, Token[CuprumContext]]
_scoped_entries: ContextVar[tuple[_ScopedEntry, ...]] = ContextVar(
    "cuprum_scoped_entries",
    default=(),
)


def _push_scoped_entry(manager: object, token: Token[CuprumContext]) -> None:
    """Record the restoration token for one scoped-manager entry."""
    _scoped_entries.set((*_scoped_entries.get(), (manager, token)))


def _reset_scoped_entry(manager: object) -> None:
    """Restore and remove the top entry when it belongs to ``manager``."""
    entries = _scoped_entries.get()
    if not entries or entries[-1][0] is not manager:
        msg = (
            "scoped context managers must exit in LIFO order in the context "
            "where they were entered"
        )
        raise RuntimeError(msg)
    try:
        _reset_context(entries[-1][1])
    except ValueError as exc:
        msg = "scoped context manager must exit in the context where it was entered"
        raise RuntimeError(msg) from exc
    _scoped_entries.set(entries[:-1])


__all__ = [
    "current_context",
    "get_context",
]
