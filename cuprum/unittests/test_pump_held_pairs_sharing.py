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

if typ.TYPE_CHECKING:
    import collections.abc as cabc

from cuprum.adapters.pump_metrics import (
    RUST_PUMP_HELD_PAIRS,
    HeldPairCounterRegistry,
    PumpMetricsHook,
    pump_metrics_hook,
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
    collector, hooks = _two_hooks_sharing_a_collector()
    gate = PausingHook()
    gate.arm()

    with observe_pump(hooks[0]), observe_pump(gate), observe_pump(hooks[1]):
        _interleave(
            gate,
            main=lambda: _emit_rust_pump_hold(1),
            other=lambda: _emit_rust_pump_hold(1),
        )
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
    collector, hooks = _two_hooks_sharing_a_collector()
    gate = PausingHook()

    with observe_pump(hooks[0]), observe_pump(gate), observe_pump(hooks[1]):
        _emit_rust_pump_hold(1)
        _emit_rust_pump_hold(1)
        gate.arm()
        _interleave(
            gate,
            main=lambda: _emit_rust_pump_hold(-1),
            other=lambda: _emit_rust_pump_hold(-1),
        )

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
    registry = HeldPairCounterRegistry()
    nested = NestedHoldEmitter()
    nested.arm(nest=True)

    with (
        observe_pump(PumpMetricsHook(collector, registry=registry)),
        observe_pump(nested),
        observe_pump(PumpMetricsHook(collector, registry=registry)),
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
    registry = HeldPairCounterRegistry()
    first = PumpMetricsHook(collector, registry=registry)
    second = PumpMetricsHook(collector, registry=registry)
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
    registry = HeldPairCounterRegistry()
    hook = PumpMetricsHook(typ.cast("typ.Any", collector), registry=registry)
    hook(PumpEvent(phase="handoff_hold", held_delta=1))
    hook(PumpEvent(phase="handoff_hold", held_delta=-1))

    assert collector in registry, "a reporting hook must register its collector"

    del hook
    gc.collect()

    assert collector not in registry, (
        "the registry must not outlive the hooks that report to the collector"
    )


def test_two_slotted_collectors_keep_separate_gauges() -> None:
    """Collectors the registry cannot weakly reference stay isolated.

    Entries are keyed by object identity, so two live collectors of the same
    type must still find one entry each rather than sharing the first one.
    """
    left = SlottedGaugeCollector()
    right = SlottedGaugeCollector()
    registry = HeldPairCounterRegistry()
    left_hook = PumpMetricsHook(typ.cast("typ.Any", left), registry=registry)
    right_hook = PumpMetricsHook(typ.cast("typ.Any", right), registry=registry)

    left_hook(PumpEvent(phase="handoff_hold", held_delta=1))
    right_hook(PumpEvent(phase="handoff_hold", held_delta=1))

    assert left.gauges == [1.0], "the left collector must count only its own hold"
    assert right.gauges == [1.0], "the right collector must count only its own hold"


def test_an_outstanding_hold_keeps_a_slotted_collectors_entry() -> None:
    """A hold outlives its reporting hook, so the count must stay reachable."""
    collector = SlottedGaugeCollector()
    registry = HeldPairCounterRegistry()
    hook = PumpMetricsHook(typ.cast("typ.Any", collector), registry=registry)
    hook(PumpEvent(phase="handoff_hold", held_delta=1))

    del hook
    gc.collect()

    assert collector in registry, "an outstanding hold must keep the count reachable"

    later = PumpMetricsHook(typ.cast("typ.Any", collector), registry=registry)
    later(PumpEvent(phase="handoff_hold", held_delta=-1))

    assert collector.gauges == [1.0, 0.0], (
        "a hook created after the reporting hook must settle the same count"
    )


def test_the_default_registry_shares_hooks_created_independently() -> None:
    """Hooks that name no registry still share their collector's total.

    The default is process-wide so an application that creates one hook per
    pipeline scope still reports the collector's total rather than the last
    hook's private count.
    """
    collector = RecordingCollector()
    first = PumpMetricsHook(collector)
    second = PumpMetricsHook(collector)

    first(PumpEvent(phase="handoff_hold", held_delta=1))
    second(PumpEvent(phase="handoff_hold", held_delta=1))

    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 2.0], (
        "hooks created without a registry must still share one running total"
    )


def test_separate_registries_keep_separate_totals() -> None:
    """An injected registry scopes the sharing it performs.

    Two hooks for one collector that name different registries hold two
    counters, which is what lets a test keep its collector out of the default
    registry's state. A caller must not use this to split one collector's
    reporting.
    """
    collector = RecordingCollector()
    first = PumpMetricsHook(collector, registry=HeldPairCounterRegistry())
    second = PumpMetricsHook(collector, registry=HeldPairCounterRegistry())

    first(PumpEvent(phase="handoff_hold", held_delta=1))
    second(PumpEvent(phase="handoff_hold", held_delta=1))

    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 1.0], (
        "each registry must keep its own running total"
    )


def test_the_factory_forwards_an_injected_registry() -> None:
    """The factory is the same injection seam as the hook constructor."""
    collector = SlottedGaugeCollector()
    registry = HeldPairCounterRegistry()
    hook = pump_metrics_hook(typ.cast("typ.Any", collector), registry=registry)

    assert collector in registry, "the factory must register with the injected registry"

    hook(PumpEvent(phase="handoff_hold", held_delta=1))

    assert collector.gauges == [1.0], "the forwarded registry must share the count"


def _two_hooks_sharing_a_collector() -> tuple[
    RecordingCollector, list[PumpMetricsHook]
]:
    """Return a collector and two hooks reporting to one shared counter."""
    collector = RecordingCollector()
    registry = HeldPairCounterRegistry()
    hooks = [
        PumpMetricsHook(collector, registry=registry),
        PumpMetricsHook(collector, registry=registry),
    ]
    return collector, hooks


def _interleave(
    gate: PausingHook,
    *,
    main: cabc.Callable[[], None],
    other: cabc.Callable[[], None],
) -> None:
    """Emit ``main`` here while ``other`` finishes on another thread.

    ``other`` runs only after the main emission has paused at ``gate``, so the
    main event is still in flight when the other thread's emission completes.
    The gate is released on every path, so a failed assertion cannot leave the
    delivery blocked behind it.
    """
    context = contextvars.copy_context()

    def emit_elsewhere() -> None:
        """Wait for the main emission to pause, then emit ``other`` and release."""
        assert gate.wait_paused(5.0), "the main delivery must pause"
        context.run(other)
        gate.release()

    worker = threading.Thread(target=emit_elsewhere)
    worker.start()
    try:
        main()
    finally:
        gate.release()
        worker.join(timeout=5.0)
    assert not worker.is_alive(), "the interleaved emission must finish"
