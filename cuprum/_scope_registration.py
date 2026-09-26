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
handle's own entry from a tuple by identity. The algorithm underneath the
second — matching by identity rather than equality, and scanning from the end so
the newest registration is the one dropped — lives in
:func:`_without_identity` so the channels cannot drift on the parts that are
easy to get subtly wrong.

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


class _TokenTupleRegistration[THook](_ScopeRegistration):
    """Base for handles that append one hook to a ``ContextVar``-backed tuple.

    A registration of this shape does one thing beyond the shared detach
    protocol: it extends the current context's hook tuple and captures the
    token that restores the tuple which preceded it. That is the same
    restoration discipline
    :class:`cuprum.context.registration._TokenRegistration` applies to
    execution contexts, and a divergent copy of it is a latent correctness
    hazard, so the channels that carry hooks in a tuple share it here rather
    than deriving it per channel.

    The tuple itself stays with each channel. A subclass passes its own
    ``ContextVar`` down, because keeping the channels on separate variables is
    what stops registering one kind of observer from altering another; only
    the append-and-restore mechanics are shared.

    Subclasses keep their own one-argument constructor and forward to this
    one, so the hook a caller passes is still the hook it validates.
    """

    __slots__ = ("_hook_var", "_token")

    def __init__(
        self,
        hook_var: ContextVar[tuple[THook, ...]],
        hook: THook,
    ) -> None:
        """Append ``hook`` to the tuple ``hook_var`` holds for this context."""
        super().__init__()
        self._hook_var = hook_var
        self._token: Token[tuple[THook, ...]] = hook_var.set((*hook_var.get(), hook))

    def _release(self) -> None:
        """Restore the tuple that preceded this registration."""
        self._hook_var.reset(self._token)


class _IdentityTupleRegistration[THook](_ScopeRegistration):
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

    The tuple itself stays with each channel, as in
    :class:`_TokenTupleRegistration`: a subclass passes its own ``ContextVar``
    down so registering one kind of observer still cannot alter another.
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
        hook_var.set((*hook_var.get(), hook))

    def _release(self) -> None:
        """Remove this registration's own hook without restoring stale state."""
        self._hook_var.set(_without_identity(self._hook_var.get(), self._hook))


__all__ = [
    "_IdentityTupleRegistration",
    "_ScopeRegistration",
    "_TokenTupleRegistration",
    "_without_identity",
]
