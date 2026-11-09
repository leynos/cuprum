"""Sharing and retirement rules of the per-collector held-pair counter.

``cuprum_rust_pump_held_pairs`` is one level per collector, so every hook
reporting to a collector must find the same counter, and one emission must move
it once however many of those hooks it reaches. The gauge's behavioural tests
cannot see the properties these tests pin: a second thread's emission arriving
between one emission's deliveries does not make the remaining deliveries count
again, a hook that emits while it is being called does not double-count the
event that called it, and the registry stops holding a collector once the hooks
that report to it are gone.

The interleaving and nesting tests deliver through ``_emit_pump_event`` rather
than by calling a hook by hand, because the counter's claim is checked against
the emitter's record of which events are in flight: only a real emission can
exercise that.
"""

from __future__ import annotations

import contextvars
import gc
import threading
import typing as typ

from cuprum.adapters._pump_held_pairs import _COUNTERS
from cuprum.adapters.pump_metrics import (
    RUST_PUMP_HELD_PAIRS,
    PumpMetricsHook,
)
from cuprum.pump_events import PumpEvent
from cuprum.pump_observation import _emit_rust_pump_hold, observe_pump
from cuprum.unittests._rust_pump_test_helpers import (
    NestedHoldEmitter,
    PausingHook,
    RecordingCollector,
    SlottedGaugeCollector,
)


def test_an_interleaved_event_does_not_move_one_hold_twice() -> None:
    """One emission moves the shared total once despite another thread's event.

    The second hook's delivery of the outer event arrives after another thread
    has completed an emission of its own. The outer event is still in flight,
    so it must be recognized as the same emission and leave the total alone.
    """
    collector = RecordingCollector()
    hooks = [PumpMetricsHook(collector), PumpMetricsHook(collector)]
    gate = PausingHook()
    gate.arm()

    with observe_pump(hooks[0]), observe_pump(gate), observe_pump(hooks[1]):
        context = contextvars.copy_context()

        def emit_elsewhere() -> None:
            """Complete an emission while the main thread's delivery waits."""
            assert gate.wait_paused(5.0), "the main delivery must pause"
            context.run(_emit_rust_pump_hold, 1)
            gate.release()

        worker = threading.Thread(target=emit_elsewhere)
        worker.start()
        try:
            _emit_rust_pump_hold(1)
        finally:
            # Never leave the gated delivery blocked behind a failed assertion.
            gate.release()
            worker.join(timeout=5.0)
        assert not worker.is_alive(), "the interleaved emission must finish"

        _emit_rust_pump_hold(-1)
        _emit_rust_pump_hold(-1)

    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 2.0, 1.0, 0.0], (
        "the second delivery of one event must not count while the first "
        "event's hold is still outstanding"
    )


def test_an_interleaved_release_does_not_move_one_hold_twice() -> None:
    """One release event moves the shared total once despite another thread's.

    The outer release reaches the first hook, then waits between the hooks
    while another thread completes a release of its own. The outer event is
    still in flight, so its remaining delivery must leave the total alone even
    though the interleaved release has already moved it.
    """
    collector = RecordingCollector()
    hooks = [PumpMetricsHook(collector), PumpMetricsHook(collector)]
    gate = PausingHook()

    with observe_pump(hooks[0]), observe_pump(gate), observe_pump(hooks[1]):
        context = contextvars.copy_context()
        _emit_rust_pump_hold(1)
        _emit_rust_pump_hold(1)
        gate.arm()

        def release_elsewhere() -> None:
            """Complete a release while the outer release waits between hooks."""
            assert gate.wait_paused(5.0), "the outer release must pause"
            context.run(_emit_rust_pump_hold, -1)
            gate.release()

        worker = threading.Thread(target=release_elsewhere)
        worker.start()
        try:
            _emit_rust_pump_hold(-1)
        finally:
            # Never leave the gated delivery blocked behind a failed assertion.
            gate.release()
            worker.join(timeout=5.0)
        assert not worker.is_alive(), "the interleaved release must finish"

    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 2.0, 1.0, 0.0], (
        "the second delivery of one release must not count while the first "
        "delivery of that event is still outstanding"
    )


def test_a_hook_that_emits_while_called_does_not_count_the_outer_hold_again() -> None:
    """A nested emission counts once and keeps the outer event's claim alive.

    The middle hook emits a second hold while the first emission is still
    being delivered, so the second metrics hook receives the outer event after
    the nested event has already moved the gauge. Both are separate holds, and
    each must move the shared total exactly once.
    """
    collector = RecordingCollector()
    nested = NestedHoldEmitter()
    nested.arm(nest=True)

    with (
        observe_pump(PumpMetricsHook(collector)),
        observe_pump(nested),
        observe_pump(PumpMetricsHook(collector)),
    ):
        _emit_rust_pump_hold(1)
        assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 2.0], (
            "the outer and the nested hold must each count once"
        )

        _emit_rust_pump_hold(-1)
        assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 2.0, 1.0, 0.0], (
            "the nested and the outer release must each count once"
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


def test_two_slotted_collectors_keep_separate_gauges() -> None:
    """Collectors the registry cannot weakly reference stay isolated.

    Entries are keyed by object identity, so two live collectors of the same
    type must still find one entry each rather than sharing the first one.
    """
    left = SlottedGaugeCollector()
    right = SlottedGaugeCollector()
    left_hook = PumpMetricsHook(typ.cast("typ.Any", left))
    right_hook = PumpMetricsHook(typ.cast("typ.Any", right))

    left_hook(PumpEvent(phase="handoff_hold", held_delta=1))
    right_hook(PumpEvent(phase="handoff_hold", held_delta=1))

    assert left.gauges == [1.0], "the left collector must count only its own hold"
    assert right.gauges == [1.0], "the right collector must count only its own hold"


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
