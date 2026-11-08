"""Sharing and retirement rules of the per-collector held-pair counter.

``cuprum_rust_pump_held_pairs`` is one level per collector, so every hook
reporting to a collector must find the same counter, and one emission must move
it once however many of those hooks it reaches. The gauge's behavioural tests
cannot see two properties these tests pin: a second thread's event arriving
between one emission's deliveries does not make the remaining deliveries count
again, and the registry stops holding a collector once the hooks that report to
it are gone.
"""

from __future__ import annotations

import gc
import threading
import typing as typ

from cuprum.adapters._pump_held_pairs import _COUNTERS
from cuprum.adapters.pump_metrics import (
    RUST_PUMP_HELD_PAIRS,
    PumpMetricsHook,
)
from cuprum.pump_events import PumpEvent
from cuprum.unittests._rust_pump_test_helpers import (
    RecordingCollector,
    SlottedGaugeCollector,
)


def test_an_interleaved_event_does_not_move_one_hold_twice() -> None:
    """One emission moves the shared total once despite another thread's event.

    ``_emit_pump_event`` hands one event to every hook inline on the emitting
    thread, so the second hook's delivery of that event can arrive after
    another thread has already delivered a different event. It must still be
    recognised as the same emission and leave the total alone.
    """
    collector = RecordingCollector()
    hooks = [PumpMetricsHook(collector), PumpMetricsHook(collector)]
    held = PumpEvent(phase="handoff_hold", held_delta=1)
    interleaved = PumpEvent(phase="handoff_hold", held_delta=1)

    def deliver_interleaved() -> None:
        """Deliver a different event to both hooks on another thread."""
        for hook in hooks:
            hook(interleaved)

    hooks[0](held)
    worker = threading.Thread(target=deliver_interleaved)
    worker.start()
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "the interleaved delivery must finish"
    hooks[1](held)

    for hook in hooks:
        hook(PumpEvent(phase="handoff_hold", held_delta=-1))

    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 2.0, 1.0, 0.0], (
        "the second delivery of one event must not count while the first "
        "event's hold is still outstanding"
    )


def test_a_retired_hook_leaves_the_surviving_hook_its_shared_total() -> None:
    """Retiring one hook's claim must not reset another hook's running total."""
    collector = RecordingCollector()
    first = PumpMetricsHook(collector)
    second = PumpMetricsHook(collector)
    first(PumpEvent(phase="handoff_hold", held_delta=1))

    del first
    gc.collect()
    second(PumpEvent(phase="handoff_hold", held_delta=1))

    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 2.0], (
        "a hook that is still alive must keep reporting the shared total"
    )


def test_the_registry_retires_a_slotted_collector_with_its_last_hook() -> None:
    """A collector without weak-reference support is not retained.

    The registry keys entries by object identity and holds the collector so
    that identity cannot be reused. Retiring the entry when its last hook is
    collected is what bounds that reference to the hooks' lifetime, which is
    the only lifetime available for a collector that cannot be weakly
    referenced.
    """
    collector = SlottedGaugeCollector()
    hook = PumpMetricsHook(typ.cast("typ.Any", collector))
    hook(PumpEvent(phase="handoff_hold", held_delta=1))
    hook(PumpEvent(phase="handoff_hold", held_delta=-1))
    key = id(collector)

    assert key in _COUNTERS, "a reporting hook must register its collector"

    del hook
    gc.collect()

    assert key not in _COUNTERS, (
        "the registry must not outlive the hooks that report to the collector"
    )


def test_an_outstanding_hold_keeps_a_slotted_collectors_entry() -> None:
    """A hold outlives its reporting hook, so the count must stay reachable."""
    collector = SlottedGaugeCollector()
    hook = PumpMetricsHook(typ.cast("typ.Any", collector))
    hook(PumpEvent(phase="handoff_hold", held_delta=1))
    key = id(collector)

    del hook
    gc.collect()

    assert key in _COUNTERS, "an outstanding hold must keep the count reachable"

    later = PumpMetricsHook(typ.cast("typ.Any", collector))
    later(PumpEvent(phase="handoff_hold", held_delta=-1))

    assert collector.gauges == [1.0, 0.0], (
        "a hook created after the reporting hook must settle the same count"
    )
