"""Share one held-pair count across the hooks reporting to a collector.

``cuprum_rust_pump_held_pairs`` is a level, not an event count, and it carries
no labels. Its scope is therefore the collector every reporting hook publishes
to: one release from one hook must not report zero while another hook still
holds a pair. This module owns that shared count and the registry that finds it
again from a collector, so ``PumpMetricsHook`` only has to translate pump events
into metric calls.
"""

from __future__ import annotations

import threading
import weakref


class _HeldPairCounter:
    """The held-pair count for one collector, shared by its reporting hooks."""

    def __init__(self) -> None:
        """Start with no held pairs under a lock that serializes publication."""
        self.lock = threading.Lock()
        self.pairs = 0
        # One event can reach several hooks that share this collector: a
        # nested ``observe_pump`` registration delivers every event to each
        # hook in both contexts, and two hooks registered at one level behave
        # the same way. The gauge is a level, so such an event must move the
        # shared total once. Keeping the event object reserves its identity
        # for the comparison; the reference ends when the next event replaces
        # it, so no history is retained.
        self.last_event: object | None = None

    def took(self, event: object) -> bool:
        """Report whether ``event`` is the one this counter applied last.

        Callers hold :attr:`lock` so the check and the update are one step.

        Returns
        -------
        bool
            Whether this counter has already applied ``event``.
        """
        return self.last_event is event


class _CounterEntry:
    """One collector's shared counter, and how its identity stays reserved."""

    __slots__ = ("collector", "counter", "reference")

    def __init__(
        self,
        counter: _HeldPairCounter,
        *,
        reference: weakref.ReferenceType[object] | None,
        collector: object | None,
    ) -> None:
        """Record the counter and the reference that keeps its key honest."""
        self.counter = counter
        self.reference = reference
        # Only a collector that cannot be weakly referenced is held here. It
        # must outlive the entry so that its ``id`` cannot name another object
        # while this counter is still registered against it.
        self.collector = collector

    def names(self, collector: object) -> bool:
        """Report whether this entry is still the one for ``collector``."""
        if self.reference is None:
            return self.collector is collector
        return self.reference() is collector


# The count belongs to the collector, not to a hook: the metric name is
# unlabelled, so every hook publishing to one collector must publish the same
# running total. Keys are object identities because a collector need not be
# hashable; an entry is retired by its weak reference, or by the strong
# reference a collector without weak-reference support needs to keep its
# identity unique. Either way there is at most one entry per collector.
_COUNTERS: dict[int, _CounterEntry] = {}
# Reentrant because a weakref callback can drop the last reference to another
# collector while this lock is already held on the collecting thread.
_COUNTERS_LOCK = threading.RLock()


def _forget_counter(key: int, reference: weakref.ReferenceType[object]) -> None:
    """Drop a retired collector's counter unless its identity was reused."""
    with _COUNTERS_LOCK:
        entry = _COUNTERS.get(key)
        if entry is not None and entry.reference is reference:
            del _COUNTERS[key]


def shared_held_counter(collector: object) -> _HeldPairCounter:
    """Return the held-pair counter every hook for ``collector`` shares.

    A release from one hook must not publish zero while another hook still
    holds a pair, so hooks with the same collector move one counter rather
    than one each. The gauge is unlabelled and collector-wide, which makes the
    collector the natural scope. A collector without weak-reference support
    shares its count too: the entry then keeps that collector alive so the
    identity used as its key cannot be reused, and the entry lasts as long as
    the collector, not as long as each hook.

    Returns
    -------
    _HeldPairCounter
        The counter this hook must move when a hold is taken or released.
    """
    key = id(collector)
    with _COUNTERS_LOCK:
        entry = _COUNTERS.get(key)
        if entry is not None and entry.names(collector):
            return entry.counter
        counter = _HeldPairCounter()
        try:
            reference = weakref.ref(
                collector,
                lambda ref, key=key: _forget_counter(key, ref),
            )
        except TypeError:
            _COUNTERS[key] = _CounterEntry(counter, reference=None, collector=collector)
            return counter
        _COUNTERS[key] = _CounterEntry(counter, reference=reference, collector=None)
        return counter
