"""Canonical base for scope-registration handles.

Every handle that installs a scoped effect and removes it again — context
registrations, hook registrations, observation registrations — exposes the same
two obligations: entering the ``with`` block must be a no-op that yields the
handle, and leaving it must detach exactly once. That protocol and its
idempotence guard are identical in every handle, so they live here rather than
being retyped per module.

What differs between handles is *only* how the effect is undone, and the
variation is real: a handle may restore a captured
:class:`~contextvars.Token`, remove its own entry from a tuple by identity, or
detach a pair of nested registrations in reverse order. Subclasses therefore
implement :meth:`_release` and nothing else about detachment; the guard that
makes a repeated detach harmless is not theirs to re-derive.

Two of those variations are large enough, and used by enough channels, to be
worth naming as bases: :class:`_TokenTupleRegistration` restores a captured
:class:`~contextvars.Token`, and :class:`_IdentityTupleRegistration` removes the
handle's own entry from a tuple by identity. Both append the caller's hook in
the same way, so that install lives once in :class:`_TupleRegistration` and only
the undo differs between them. The algorithm underneath the second — matching by
identity rather than equality, and scanning from the end so the newest
registration is the one dropped — lives in :func:`_without_identity` so the
channels cannot drift on the parts that are easy to get subtly wrong.

Detach runs exactly once per handle. ``_release`` raising leaves ``_detached``
false, so a handle whose release refuses (see
:class:`~cuprum.pump_span_observation.PumpHopSpanRegistration`, which rejects an
out-of-order detach) stays retryable rather than being marked done.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from contextvars import ContextVar, Token


def _without_identity[T](hooks: tuple[T, ...], target: T) -> tuple[T, ...]:
    """Return *hooks* with the most recent entry that is *target* removed.

    Matching is by identity, not equality: a hook is removed only when it is
    the very object this registration installed, so an equal-but-distinct hook
    registered by someone else is left alone. The scan runs from the end so a
    hook registered twice loses its newest registration, which keeps detach
    working in last-in-first-out order. A *target* that is absent leaves
    *hooks* unchanged, so a repeated detach is harmless.

    Returns
    -------
    tuple[_T, ...]
        The remaining hooks, in their original order.
    """
    remaining = list(hooks)
    for index in range(len(remaining) - 1, -1, -1):
        if remaining[index] is target:
            del remaining[index]
            break
    return tuple(remaining)


class _ScopeRegistration:
    """Base owning the context-manager protocol and detach idempotence.

    Subclasses declare ``_detached`` in their own ``__slots__`` (or, for
    dataclass handles, as a default-``False`` field), initialize it through
    :meth:`__init__`, and implement :meth:`_release`.
    """

    __slots__ = ("_detached",)

    def __init__(self) -> None:
        """Initialize the handle in the attached, undetached state."""
        self._detached = False

    def _release(self) -> None:
        """Undo this registration's effect.

        Called at most once, by :meth:`detach`.

        Raises
        ------
        NotImplementedError
            Always, unless a subclass overrides it.
        """
        msg = f"{type(self).__name__} must implement _release()"
        raise NotImplementedError(msg)

    def detach(self) -> None:
        """Release the registration, exactly once."""
        if self._detached:
            return
        self._release()
        self._detached = True

    def __enter__(self) -> typ.Self:
        """Enter the registration scope; the effect is already installed."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Detach the registration on scope exit."""
        self.detach()


class _TupleRegistration[THook](_ScopeRegistration):
    """Base for handles that append one hook to a ``ContextVar``-backed tuple.

    Every registration of this shape does the same install, so it lives here
    once: extend the hook tuple the channel's ``ContextVar`` holds for the
    current context, and keep both the variable and the hook so the undo can
    reach them. What differs between the two undo disciplines is *only* how the
    tuple is put back, which each subclass implements in :meth:`_release`.

    The tuple itself stays with each channel. A subclass passes its own
    ``ContextVar`` down, because keeping the channels on separate variables is
    what stops registering one kind of observer from altering another; only the
    append mechanics are shared.

    Subclasses keep their own one-argument constructor and forward to this
    one, so the hook a caller passes is still the hook it validates.
    """

    __slots__ = ("_hook", "_hook_var")

    def __init__(
        self,
        hook_var: ContextVar[tuple[THook, ...]],
        hook: THook,
    ) -> None:
        """Append ``hook`` to the tuple ``hook_var`` holds for this context."""
        super().__init__()
        self._hook_var = hook_var
        self._hook = hook
        self._append()

    def _append(self) -> None:
        """Extend this channel's hook tuple with the registered hook.

        Set apart from ``__init__`` because
        :meth:`~contextvars.ContextVar.set` returns the token that restores
        the preceding tuple, and only a subclass whose detach *is* that
        restoration may keep it. The base discards the token deliberately, so
        an identity-based subclass inherits no stale restoration state that
        its detach must not use.
        """
        self._hook_var.set((*self._hook_var.get(), self._hook))


class _TokenTupleRegistration[THook](_TupleRegistration[THook]):
    """Base for handles that restore a captured ``Token`` on detach.

    A registration of this shape does one thing beyond the shared detach
    protocol: it captures the token that restores the tuple which preceded it.
    That is the same restoration discipline
    :class:`cuprum.context.registration._TokenRegistration` applies to
    execution contexts, and a divergent copy of it is a latent correctness
    hazard, so the channels that carry hooks in a tuple share it here rather
    than deriving it per channel.

    Because :meth:`_release` restores the exact preceding tuple, a detach that
    runs out of last-in-first-out order discards whatever later registrations
    added. That ordering is the caller's responsibility; the sibling
    :class:`_IdentityTupleRegistration` exists for channels that cannot
    guarantee it.
    """

    __slots__ = ("_token",)

    def _append(self) -> None:
        """Append the hook and capture the token that restores the prior tuple."""
        self._token: Token[tuple[THook, ...]] = self._hook_var.set((
            *self._hook_var.get(),
            self._hook,
        ))

    def _release(self) -> None:
        """Restore the tuple that preceded this registration."""
        self._hook_var.reset(self._token)


class _IdentityTupleRegistration[THook](_TupleRegistration[THook]):
    """Base for handles that append a hook to a tuple and remove it by identity.

    The sibling of :class:`_TokenTupleRegistration`, and the same restoration
    discipline read the other way round. Where a token restores the exact tuple
    that preceded the registration, this removes only the handle's own entry and
    leaves whatever else arrived in between. The channels that need it do so
    because their detach is *not* guaranteed to unwind in last-in-first-out
    order: a caller that detaches a registration out of order must not
    resurrect a hook someone already removed, and token restoration would do
    exactly that.

    Sharing it here keeps that guarantee from being re-derived per channel. The
    identity match and the end-first scan are in :func:`_without_identity`, so
    a channel cannot get the subtle half right and the other half wrong.
    """

    __slots__ = ()

    def _release(self) -> None:
        """Remove this registration's own hook without restoring stale state."""
        self._hook_var.set(_without_identity(self._hook_var.get(), self._hook))


__all__ = [
    "_IdentityTupleRegistration",
    "_ScopeRegistration",
    "_TokenTupleRegistration",
    "_TupleRegistration",
    "_without_identity",
]
