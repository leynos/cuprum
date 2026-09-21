"""Scoped execution contexts derived from explicit configuration or catalogues."""

from __future__ import annotations

import typing as typ

from cuprum.catalogue import (
    ProgramCatalogue,  # ruff: ignore[typing-only-first-party-import] - public annotations must resolve at runtime,
)
from cuprum.context.core import CuprumContext, ScopeConfig
from cuprum.context.state import _reset_context, _set_context, current_context

if typ.TYPE_CHECKING:
    from contextvars import Token


class _ScopedContext:
    """Context manager for entering a scoped execution context."""

    __slots__ = ("_ctx", "_token")

    def __init__(self, config: ScopeConfig) -> None:
        """Narrow the current context with ``config`` for later entry."""
        parent = current_context()
        self._ctx = parent.narrow(config)
        self._token: Token[CuprumContext] | None = None

    def __enter__(self) -> CuprumContext:
        """Activate the scoped context and return it."""
        self._token = _set_context(self._ctx)
        return self._ctx

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Restore the previous context on scope exit."""
        if self._token is not None:
            _reset_context(self._token)


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
        Catalogue whose allowlist establishes the scope. Mutually exclusive
        with ``config``.

    Returns
    -------
    _ScopedContext
        A context manager that narrows the current context.

    Example
    -------
    >>> with scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as ctx:
    ...     assert ctx.is_allowed(ECHO)
    >>> catalogue = ProgramCatalogue.from_programs(ECHO)
    >>> with scoped(catalogue=catalogue) as ctx:
    ...     assert ctx.is_allowed(ECHO)

    Raises
    ------
    TypeError
        If neither, or both, ``config`` and ``catalogue`` are supplied.
    """
    if config is not None:
        if catalogue is not None:
            msg = "scoped() accepts either config or catalogue, not both"
            raise TypeError(msg)
        return _ScopedContext(config)
    if catalogue is None:
        msg = "scoped() requires config or catalogue"
        raise TypeError(msg)
    return _ScopedContext(ScopeConfig(allowlist=catalogue.allowlist))
