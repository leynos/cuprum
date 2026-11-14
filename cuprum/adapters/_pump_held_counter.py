"""The held-pair count one collector's reporting hooks share.

``cuprum_rust_pump_held_pairs`` is a level, not an event count, and it carries
no labels. Its scope is therefore the collector every reporting hook publishes
to: one release from one hook must not report zero while another hook still
holds a pair. This module owns that count and the claim that makes one emission
move it once, so ``PumpMetricsHook`` only has to translate pump events into
metric calls.

Which hooks share a counter, and how long it outlives them, is
:mod:`cuprum.adapters._pump_held_registry`'s concern.
"""

from __future__ import annotations

import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.pump_events import PumpEmission, PumpEvent


class _HeldPairCounter:
    """The held-pair count for one collector, shared by its reporting hooks."""

    def __init__(self) -> None:
        """Start with no held pairs under a lock that serializes publication."""
        self._lock = threading.Lock()
        self.pairs = 0
        # Emissions whose deliveries have already moved this count. One
        # emission delivers its event to every hook in the current context,
        # inline on the thread that emitted it, and nested ``observe_pump``
        # registrations put several of those deliveries on the same counter, so
        # one emission must move the shared total once. A claim is dropped as
        # soon as its emission is closed, which bounds the list by the nesting
        # depth in flight rather than by the number of events seen.
        self._claimed: list[PumpEmission] = []

    def claim_delivery(self, event: PumpEvent) -> bool:
        """Record that this delivery may move the count, and say whether it does.

        This is a command, not a query: a delivery it accepts is added to the
        claims, and every later delivery carrying the same emission is refused
        until that emission is closed. Callers hold :attr:`_lock`, so a claim
        and the update it authorizes are one step.

        An event with no emission is one a caller built and handed to a hook
        itself. It has no delivery identity to share, so every such delivery
        counts: each is a separate act by its caller, not a repeat of one
        emission.

        Returns
        -------
        bool
            Whether this delivery should move the count.
        """
        emission = event.emission
        if emission is None:
            return True
        self._claimed[:] = [seen for seen in self._claimed if not seen.is_closed]
        if any(seen is emission for seen in self._claimed):
            return False
        self._claimed.append(emission)
        return True

    def settle(
        self,
        event: PumpEvent,
        *,
        delta: int,
        publish: cabc.Callable[[int], None],
    ) -> None:
        """Move the shared count by ``delta`` and publish the new total once.

        ``delta`` is a balanced ``+1`` acquisition or ``-1`` release; callers
        validate it before calling. ``publish`` receives the running total and
        is called only for a delivery :meth:`claim_delivery` accepted.

        One lock guards the claim, the update, and the publication, so two
        threads cannot publish out of order: a release that computed ``0``
        could otherwise reach the collector first, and the acquisition it
        followed would then overwrite that with a stale ``1`` that no later
        event corrects. Publishing while holding the lock is safe because
        ``publish`` is a collector call that must record the value and return.

        Lock ordering is trivial because the two locks in this adapter family
        are never nested: this lock guards only the count, the registry lock
        guards only its entries, and neither is taken while the other is held.
        That is deliberate, not incidental — the registry retires an entry from
        a weak-reference finalizer, which can run at any allocation, including
        inside ``publish``; a finalizer that waited on this lock could therefore
        deadlock against the very callback that triggered it.
        """
        with self._lock:
            if not self.claim_delivery(event):
                return
            self.pairs += delta
            publish(self.pairs)
