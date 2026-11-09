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

from cuprum.pump_observation import _in_flight_pump_events


class _HeldPairCounter:
    """The held-pair count for one collector, shared by its reporting hooks."""

    def __init__(self) -> None:
        """Start with no held pairs under a lock that serializes publication."""
        self.lock = threading.Lock()
        self.pairs = 0
        # Events this thread has already moved the count for. One emission
        # delivers its event to every hook in the current context, inline on
        # the thread that emitted it, and a nested ``observe_pump``
        # registration puts several of those deliveries on the same counter, so
        # one event must move the shared total once.
        #
        # A claim lasts exactly as long as the emission it was made for: an
        # event remains claimed only while its delivery loop is still running
        # on this thread. That covers a hook which emits an event of its own
        # while it is being called — the nested event counts separately, while
        # the outer event's remaining deliveries are still recognized — and it
        # keeps the claim bounded by the nesting depth rather than growing with
        # every event seen. The claims are per thread because another thread's
        # event must not disturb them, and the storage retires with its thread.
        self._claimed = threading.local()

    def admit(self, event: object) -> bool:
        """Claim ``event`` for this thread and say whether it moves the count.

        Callers hold :attr:`lock`, so the claim and the update it authorizes
        are one step. Within one thread, a delivery that repeats an event whose
        emission is still in flight is the only repeat this refuses: re-entering
        the emitter nests a new event, and that event counts on its own.

        Returns
        -------
        bool
            Whether this delivery should move the count.
        """
        in_flight = _in_flight_pump_events()
        claimed: list[object] | None = getattr(self._claimed, "events", None)
        if claimed is None:
            claimed = self._claimed.events = []
        # Drop the claims whose emission this thread has finished delivering.
        # A claim can only ever be queried while its event is in flight, so
        # discarding the rest loses nothing and keeps this list bounded.
        claimed[:] = [
            seen for seen in claimed if any(seen is live for live in in_flight)
        ]
        if any(seen is event for seen in claimed):
            return False
        claimed.append(event)
        return True


class _CounterEntry:
    """One collector's shared counter, and how its identity stays reserved."""

    __slots__ = ("collector", "counter", "hooks", "reference")

    def __init__(
        self,
        counter: _HeldPairCounter,
        *,
        reference: weakref.ReferenceType[object] | None,
        collector: object | None,
    ) -> None:
        """Record the counter and what keeps its key honest."""
        self.counter = counter
        self.reference = reference
        # Only a collector that cannot be weakly referenced is held here. It
        # must outlive the entry so that its ``id`` cannot name another object
        # while this counter is still registered against it.
        self.collector = collector
        # Hooks reporting through this entry. Only a retained entry uses the
        # count: an entry with a weak reference is retired by the collector.
        self.hooks = 0

    def names(self, collector: object) -> bool:
        """Report whether this entry is still the one for ``collector``."""
        if self.reference is None:
            return self.collector is collector
        return self.reference() is collector


# The count belongs to the collector, not to a hook: the metric name is
# unlabelled, so every hook publishing to one collector must publish the same
# running total. Keys are object identities because a collector need not be
# hashable. A collector that can be weakly referenced retires its entry when it
# dies, so a hold can outlive the hook that reported it. A collector that
# cannot give that signal is held by its entry instead — its identity must stay
# unique — and the entry is retired once the last hook reporting to it is gone
# and nothing is held, which is the only end of use that is observable.
_COUNTERS: dict[int, _CounterEntry] = {}
# Reentrant because a weak-reference callback can drop the last reference to
# another collector while this lock is already held on the collecting thread.
_COUNTERS_LOCK = threading.RLock()


def _forget_counter(key: int, reference: weakref.ReferenceType[object]) -> None:
    """Drop a retired collector's counter unless its identity was reused."""
    with _COUNTERS_LOCK:
        entry = _COUNTERS.get(key)
        if entry is not None and entry.reference is reference:
            del _COUNTERS[key]


def _new_counter_entry(key: int, collector: object) -> _CounterEntry:
    """Record a fresh counter for ``collector`` and return its entry.

    Callers hold :data:`_COUNTERS_LOCK`, so the identity read here cannot be
    claimed by another registration before the entry is stored.

    Returns
    -------
    _CounterEntry
        The entry recorded under ``key``.
    """
    counter = _HeldPairCounter()
    try:
        reference = weakref.ref(
            collector,
            lambda ref, key=key: _forget_counter(key, ref),
        )
    except TypeError:
        entry = _CounterEntry(counter, reference=None, collector=collector)
    else:
        entry = _CounterEntry(counter, reference=reference, collector=None)
    _COUNTERS[key] = entry
    return entry


def _claim_entry(key: int, collector: object) -> _CounterEntry:
    """Return the entry for ``collector``, adding one hook's claim.

    Callers hold :data:`_COUNTERS_LOCK`.

    Returns
    -------
    _CounterEntry
        The entry the claiming hook reports through.
    """
    entry = _COUNTERS.get(key)
    if entry is None or not entry.names(collector):
        entry = _new_counter_entry(key, collector)
    entry.hooks += 1
    return entry


def _retire_held_counter(key: int, counter: _HeldPairCounter) -> None:
    """Drop one hook's claim, retiring a retained entry with its last one."""
    with _COUNTERS_LOCK:
        entry = _COUNTERS.get(key)
        # The entry can already be gone while the interpreter shuts down, and
        # another registration can have replaced it with a fresh counter.
        if entry is None or entry.counter is not counter:
            return
        # An entry with a weak reference retires with its collector instead.
        if entry.reference is not None:
            return
        entry.hooks -= 1
        if entry.hooks <= 0 and counter.pairs == 0:
            del _COUNTERS[key]


def register_held_counter(hook: object, collector: object) -> _HeldPairCounter:
    """Return ``collector``'s shared counter and tie it to ``hook``'s lifetime.

    A collector that can be weakly referenced gives the entry its own end of
    use, so a hold can outlive the hook that reported it. A collector that
    cannot is kept by its entry — the identity used as the key must stay
    unique — and the entry ends when ``hook`` and every other hook reporting to
    that collector have been collected, provided nothing is still held.

    Parameters
    ----------
    hook:
        The reporting hook whose claim this registration records. It must be
        weak-referenceable.
    collector:
        The collector the hook publishes to. A collector need not be hashable
        or weakly referenceable.

    Returns
    -------
    _HeldPairCounter
        The counter this hook must move when a hold is taken or released.

    """
    key = id(collector)
    with _COUNTERS_LOCK:
        counter = _claim_entry(key, collector).counter
    weakref.finalize(hook, _retire_held_counter, key, counter)
    return counter
