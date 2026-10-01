"""The canonical ContextVar-backed registration handle base.

Every scope-registration handle in :mod:`cuprum.context` derives from
:class:`_TokenRegistration`. It lives in its own module rather than beside the
public factories because the handles are split across
:mod:`cuprum.context.registration` and
:mod:`cuprum.context._env_registration`, and both need the base without
importing each other.

The base owns the whole token-restoration discipline: subclasses perform only
the context-derivation step in ``__init__`` and hand the derived context to
:meth:`_TokenRegistration._install`, so the subtle part cannot drift between
handle types.
"""

from __future__ import annotations

import typing as typ

from cuprum.context.state import _reset_context, _set_context

if typ.TYPE_CHECKING:
    from contextvars import Token

    from cuprum.context.core import CuprumContext

__all__ = ["_TokenRegistration"]


class _TokenRegistration:
    """Canonical base for ContextVar-backed scope-registration handles.

    All scope-registration handles (allowlist extensions, hook
    registrations, env overlays) derive from this base. Subclasses perform
    only the context-derivation step in ``__init__`` and hand the derived
    context to :meth:`_install`; the token capture, idempotent
    :meth:`detach`, and context-manager protocol live here so the subtle
    restoration discipline cannot drift between handle types.

    Token-based Restoration
    -----------------------
    The registration captures a :class:`~contextvars.Token` when the derived
    context is installed. When :meth:`detach` is called, the original context
    is restored via the token, ensuring no context pollution even when used
    outside ``scoped(ScopeConfig())`` blocks. This means :meth:`detach`
    restores the exact context that existed when the registration was
    created, regardless of subsequent context modifications. If multiple
    registrations are created and detached in non-LIFO (last in, first out)
    order, earlier tokens restore states that discard changes layered by
    later registrations; prefer ``with`` blocks, which detach in LIFO order.

    Detach in the same logical :class:`~contextvars.Context` (thread or
    task) in which the registration was created. Resetting a
    :class:`~contextvars.ContextVar` with a token from a different context
    raises :class:`ValueError`.
    """

    __slots__ = ("_detached", "_token")

    def __init__(self) -> None:
        """Initialize the handle in the attached, token-less state."""
        self._detached = False
        self._token: Token[CuprumContext] | None = None

    def _install(self, new_ctx: CuprumContext) -> None:
        """Set ``new_ctx`` as current and capture the restoration token."""
        self._token = _set_context(new_ctx)

    def detach(self) -> None:
        """Restore the original context via the captured token."""
        if self._detached:
            return
        if self._token is not None:
            _reset_context(self._token)
            self._token = None
        self._detached = True

    def __enter__(self) -> typ.Self:
        """Enter context manager; the registration is already installed."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit context manager; detach the registration."""
        self.detach()
