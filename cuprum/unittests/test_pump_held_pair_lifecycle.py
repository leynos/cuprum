"""Lifecycle of a held-pair counter for a weakly referenceable collector.

The counter belongs to the collector, and the gauge it drives is a level, so a
hold must stay reachable after the hook that reported it is gone: only the
collector's own end of use may retire it. These tests pin that rule for a
collector the registry can weakly reference — the case the slotted collector
cannot exercise, because it gives the registry no death signal. A retained
entry must not be what keeps such a collector, or the counter behind it, in
memory.
"""

from __future__ import annotations

import gc
import weakref

from cuprum.adapters.pump_metrics import (
    RUST_PUMP_HELD_PAIRS,
    HeldPairCounterRegistry,
    PumpMetricsHook,
)
from cuprum.pump_events import PumpEvent
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector


class _HookProbe:
    """A weak-referenceable stand-in for a hook that reports a hold."""


def test_a_later_hook_settles_a_hold_left_by_a_collected_hook() -> None:
    """A live collector keeps its count after its only hook is collected."""
    collector = RecordingCollector()
    registry = HeldPairCounterRegistry()
    hook = PumpMetricsHook(collector, registry=registry)
    hook(PumpEvent(phase="handoff_hold", held_delta=1))

    del hook
    gc.collect()

    assert collector in registry, "the live collector must keep its count"

    later = PumpMetricsHook(collector, registry=registry)
    later(PumpEvent(phase="handoff_hold", held_delta=-1))

    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 0.0], (
        "a hook created after the reporting hook must settle the same count"
    )


def test_an_outstanding_hold_does_not_outlive_a_dead_collector() -> None:
    """The entry retires with the collector while a hold is still outstanding.

    Registering through the public seam returns the shared counter, which is
    how this test observes retirement: the counter must be released with the
    entry, not kept by a hold that no collector can settle any more.
    """
    collector = RecordingCollector()
    registry = HeldPairCounterRegistry()
    probe = _HookProbe()
    counter_ref = weakref.ref(registry.register(probe, collector))
    hook = PumpMetricsHook(collector, registry=registry)
    hook(PumpEvent(phase="handoff_hold", held_delta=1))

    del hook, probe
    gc.collect()

    assert counter_ref() is not None, "a live collector must keep its counter"

    collector_ref = weakref.ref(collector)
    del collector
    gc.collect()

    assert collector_ref() is None, "the registry must not retain the collector"
    assert counter_ref() is None, "the counter must retire with its collector"
