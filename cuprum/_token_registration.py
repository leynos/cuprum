"""Shared token-restoration lifecycle for ``ContextVar``-backed handles.

Cuprum registers scoped state on two independent channels: execution-context
handles in :mod:`cuprum.context.registration`, and pump-observation handles in
:mod:`cuprum.pump_observation`. Both install a value on a
:class:`~contextvars.ContextVar`, keep the :class:`~contextvars.Token` the set
returned, and restore the previous value on detach. That lifecycle is short but
unforgiving: a divergence between the two copies surfaces as a leaked context
long after the code that leaked it, so it is owned here once.

The base is generic over the variable's value type and imports nothing from
either channel. That neutrality is the point rather than a convenience:
ADR-008 records that pump observation deliberately keeps its own
``ContextVar`` instead of riding on :class:`~cuprum.context.CuprumContext`, and
sharing an implementation must not smuggle a dependency on
:mod:`cuprum.context` back into the observation channel.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from contextvars import ContextVar, Token


class _TokenRegistrationBase[T]:
    """Canonical token-restoration lifecycle for one ``ContextVar`` value.

    Subclasses bind the variable in ``__init__``, derive the value to install,
    and hand it to :meth:`_install`. Token capture, idempotent :meth:`detach`,
    and the context-manager protocol live here so the restoration discipline
    cannot drift between handle types.

    Token-based restoration
    -----------------------
    The registration captures a :class:`~contextvars.Token` when it installs a
    value. :meth:`detach` restores the exact value that was current when the
    registration was created, regardless of later modifications. Registrations
    detached out of last-in-first-out order therefore restore snapshots that
    discard values layered by later registrations; prefer ``with`` blocks,
    which detach in LIFO order.

    Detach in the same logical :class:`~contextvars.Context` (thread or task)
    in which the registration was created. Resetting a
    :class:`~contextvars.ContextVar` with a token from a different context
    raises :class:`ValueError`.
    """

    __slots__ = ("_detached", "_token", "_var")

    def __init__(self, var: ContextVar[T]) -> None:
        """Bind the handle to ``var`` in the attached, token-less state."""
        self._var = var
        self._detached = False
        self._token: Token[T] | None = None

    def _install(self, new_value: T) -> None:
        """Set ``new_value`` on the bound variable, capturing its token."""
        self._token = self._var.set(new_value)

    def detach(self) -> None:
        """Restore the value that preceded this registration.

        The handle is marked detached only once the reset has succeeded. A
        cross-context detach raises :class:`ValueError` from
        :meth:`~contextvars.ContextVar.reset`; leaving the flag and the token
        untouched lets the owning context retry, rather than stranding the
        installed value behind a handle that believes it is already detached.
        """
        if self._detached:
            return
        if self._token is not None:
            self._var.reset(self._token)
            self._token = None
        self._detached = True

    def __enter__(self) -> typ.Self:
        """Enter the context manager; the registration is already installed."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit the context manager; detach the registration."""
        self.detach()


__all__ = ["_TokenRegistrationBase"]
