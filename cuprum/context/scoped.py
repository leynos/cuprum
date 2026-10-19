"""Create entry-time narrowed execution contexts from configuration or catalogues.

``scoped`` accepts either a ``ScopeConfig`` or a ``ProgramCatalogue`` and
returns a context manager that derives and activates a narrowed
``CuprumContext`` on each entry. A context-local entry stack pairs each use with
its own restoration token, including when a manager is reused or nested.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from cuprum.catalogue import (
    ProgramCatalogue,  # ruff: ignore[typing-only-first-party-import] - public annotations must resolve at runtime,
)
from cuprum.context.core import CuprumContext, ScopeConfig
from cuprum.context.state import _reset_context, _set_context, current_context


class _ScopedContext:
    """Context manager that derives its execution policy on each entry."""

    __slots__ = ("_config",)

    def __init__(self, config: ScopeConfig) -> None:
        """Store the validated configuration for later entry."""
        self._config = config

    def __enter__(self) -> CuprumContext:
        """Narrow the active context, install it, and return the result."""
        context = current_context().narrow(self._config)
        token = _set_context(context)
        _scoped_entries.set((*_scoped_entries.get(), (self, token)))
        return context

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Restore this entry's parent context in last-in, first-out order."""
        entries = _scoped_entries.get()
        if not entries or entries[-1][0] is not self:
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


_scoped_entries: ContextVar[tuple[tuple[_ScopedContext, Token[CuprumContext]], ...]] = (
    ContextVar("cuprum_scoped_entries", default=())
)


def scoped(
    config: ScopeConfig | None = None,
    *,
    catalogue: ProgramCatalogue | None = None,
) -> _ScopedContext:
    """Create a scoped context manager for narrowed execution.

    Parameters
    ----------
    config:
        Scope configuration describing allowlist and hook updates. Mutually
        exclusive with ``catalogue``.
    catalogue:
        Catalogue whose allowlist establishes the scope and whose program
        entries ``sh.make`` resolves inside it. Mutually exclusive with
        ``config``.

    Returns
    -------
    _ScopedContext
        A reusable context manager that narrows the context active at entry.

    Raises
    ------
    TypeError
        If neither, or both, ``config`` and ``catalogue`` are supplied.

    Notes
    -----
    Each entry derives policy from the context active at that moment. Nested and
    sequential entries are safe, and each entry must exit in LIFO order.

    Example
    -------
    >>> with scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as ctx:
    ...     assert ctx.is_allowed(ECHO)
    >>> catalogue = ProgramCatalogue.from_programs(ECHO)
    >>> with scoped(catalogue=catalogue) as ctx:
    ...     assert ctx.is_allowed(ECHO)
    """
    if config is not None:
        if catalogue is not None:
            msg = "scoped() accepts either config or catalogue, not both"
            raise TypeError(msg)
        return _ScopedContext(config)
    if catalogue is None:
        msg = "scoped() requires config or catalogue"
        raise TypeError(msg)
    return _ScopedContext(
        ScopeConfig(allowlist=catalogue.allowlist, catalogue=catalogue)
    )
