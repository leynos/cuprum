"""Create entry-time narrowed execution contexts from configuration or catalogues.

``scoped`` accepts either a ``ScopeConfig`` or a ``ProgramCatalogue`` and
returns a context manager that derives and activates a narrowed
``CuprumContext`` on each entry. A context-local entry stack pairs each use with
its own restoration token, including when a manager is reused or nested.
"""

from __future__ import annotations

from cuprum.catalogue import (
    ProgramCatalogue,  # ruff: ignore[typing-only-first-party-import] - public annotations must resolve at runtime,
)
from cuprum.context.core import CuprumContext, ScopeConfig
from cuprum.context.state import (
    _push_scoped_entry,
    _reset_scoped_entry,
    _set_context,
    current_context,
)


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
        _push_scoped_entry(self, token)
        return context

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Restore this entry's parent context in last-in, first-out order."""
        _reset_scoped_entry(self)


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
