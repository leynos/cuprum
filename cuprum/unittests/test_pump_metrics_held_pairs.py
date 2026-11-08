"""The held-pair gauge reported by ``PumpMetricsHook``.

The gauge answers a question the counters cannot: how many descriptor pairs a
hand-off is holding while its buffered drain is still pending. These tests pin
the bounded accumulation, the optional-collector contract, the balance across
concurrent drains, and the single running total every hook reporting to one
collector shares.
"""

from __future__ import annotations

import contextvars
import threading
import typing as typ

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from cuprum.adapters.gauge_metrics import GaugeMetricsCollector
from cuprum.adapters.metrics_adapter import InMemoryMetrics
from cuprum.adapters.pump_metrics import (
    RUST_PUMP_DECLINED_TOTAL,
    RUST_PUMP_HELD_PAIRS,
    PumpMetricsHook,
)
from cuprum.pump_events import PumpEvent, RustPumpDeclineReason
from cuprum.pump_observation import _emit_pump_event, observe_pump
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _hold(delta: int) -> PumpEvent:
    """Build the hold transition the coordinator emits."""
    return PumpEvent(phase="handoff_hold", held_delta=delta)


def test_a_hold_raises_and_returns_the_gauge() -> None:
    """One acquisition and one release leave the gauge at zero."""
    collector = RecordingCollector()
    hook = PumpMetricsHook(collector)

    hook(_hold(1))
    hook(_hold(-1))

    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 0.0], (
        "the gauge must report the held pair and its release"
    )


class _TwoMethodCollector:
    """A collector written before gauges existed, with no ``set_gauge``."""

    def __init__(self) -> None:
        """Start with no recorded counters."""
        self.counters: list[str] = []

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record the counter name."""
        del value, labels
        self.counters.append(name)

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Ignore histogram observations."""
        del name, value, labels


def test_a_collector_without_gauges_still_receives_counters() -> None:
    """Gauge support is optional, so an unchanged collector keeps working."""
    collector = _TwoMethodCollector()
    hook = PumpMetricsHook(typ.cast("typ.Any", collector))

    hook(_hold(1))
    hook(_hold(-1))
    hook(PumpEvent(phase="declined", reason=RustPumpDeclineReason.RAW_FD_UNAVAILABLE))

    assert collector.counters == [RUST_PUMP_DECLINED_TOTAL], (
        "a collector without a gauge must still receive the counters it supports"
    )


@pytest.mark.parametrize("delta", [0, 2, -2], ids=["zero", "double", "negative"])
def test_a_malformed_hold_delta_is_ignored(delta: int) -> None:
    """A hand-built event cannot move the gauge by an unbounded amount."""
    collector = RecordingCollector()

    PumpMetricsHook(collector)(_hold(delta))

    assert not collector.gauges, (
        f"only a single acquisition or release may move the gauge, found {delta}"
    )


def test_concurrent_drains_accumulate_under_one_hook() -> None:
    """Four drains held at once are reported as four held pairs."""
    collector = RecordingCollector()
    hook = PumpMetricsHook(collector)
    held_by_all = threading.Barrier(4)

    def hold_and_release() -> None:
        """Hold a pair until every drain holds one, then release it."""
        hook(_hold(1))
        held_by_all.wait(timeout=5.0)
        hook(_hold(-1))

    workers = [threading.Thread(target=hold_and_release) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5.0)

    values = collector.gauge_values(RUST_PUMP_HELD_PAIRS)
    assert max(values) == pytest.approx(4.0), (
        f"every concurrently held pair must be counted, found {values}"
    )
    assert values[-1] == pytest.approx(0.0), (
        f"the gauge must return to zero, found {values}"
    )
    assert all(0.0 <= value <= 4.0 for value in values), (
        f"the gauge must stay bounded by the drains in flight, found {values}"
    )


class _GatedGaugeCollector(RecordingCollector):
    """Hold the first publication so a competing release can overtake it."""

    def __init__(self) -> None:
        """Start with no recorded gauges and a closed publication gate."""
        super().__init__()
        self.publishing = threading.Event()
        self.second_publication = threading.Event()
        self.publication_gate = threading.Event()
        self._is_first_publication = True
        self._first_publication_lock = threading.Lock()

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Block inside the first publication, before it records its value."""
        with self._first_publication_lock:
            is_first = self._is_first_publication
            self._is_first_publication = False
        if not is_first:
            self.second_publication.set()
            super().set_gauge(name, value, labels)
            return
        self.publishing.set()
        assert self.publication_gate.wait(timeout=5.0), (
            "the test must open the publication gate"
        )
        super().set_gauge(name, value, labels)


def test_an_older_publication_cannot_overwrite_a_newer_count() -> None:
    """A slow acquisition must not publish a stale count after its release."""
    collector = _GatedGaugeCollector()
    hook = PumpMetricsHook(collector)
    release_started = threading.Event()

    def acquire() -> None:
        """Take the hold whose publication the test will interleave with."""
        hook(_hold(1))

    def release() -> None:
        """Release the hold while the acquisition is still publishing."""
        release_started.set()
        hook(_hold(-1))

    acquirer = threading.Thread(target=acquire)
    releaser = threading.Thread(target=release)
    acquirer.start()
    assert collector.publishing.wait(timeout=5.0), (
        "the acquisition must reach the collector before the release starts"
    )
    releaser.start()
    try:
        assert release_started.wait(timeout=5.0), (
            "the release thread must start before the gate opens"
        )
        assert not collector.second_publication.wait(timeout=0.2), (
            "the release must wait for the acquisition it follows to publish first"
        )
    finally:
        # Always release the blocked publication so a failed expectation
        # cannot leave the collector's first caller waiting.
        collector.publication_gate.set()
    acquirer.join(timeout=5.0)
    releaser.join(timeout=5.0)

    assert not acquirer.is_alive(), "the acquisition must finish"
    assert not releaser.is_alive(), "the release must finish"
    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 0.0], (
        "the release must publish after the acquisition it followed, never before"
    )


def test_hooks_sharing_a_collector_publish_one_running_total() -> None:
    """A release through one hook must not hide another hook's hold."""
    collector = RecordingCollector()
    first = PumpMetricsHook(collector)
    second = PumpMetricsHook(collector)

    first(_hold(1))
    second(_hold(1))
    first(_hold(-1))
    second(_hold(-1))

    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 2.0, 1.0, 0.0], (
        "the gauge must count every pair held through either hook"
    )


def test_distinct_collectors_keep_separate_gauges() -> None:
    """The count belongs to its collector, not to the process."""
    left = RecordingCollector()
    right = RecordingCollector()

    PumpMetricsHook(left)(_hold(1))
    PumpMetricsHook(right)(_hold(1))
    PumpMetricsHook(left)(_hold(1))

    assert left.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 2.0], (
        "each collector must report only the pairs held through its own hooks"
    )
    assert right.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0], (
        "a second collector must not inherit the first collector's count"
    )


def test_concurrent_hooks_on_one_collector_share_the_gauge() -> None:
    """Two drains held through different hooks are reported as two pairs."""
    collector = RecordingCollector()
    hooks = [PumpMetricsHook(collector) for _ in range(2)]
    both_held = threading.Barrier(len(hooks))

    def hold_and_release(hook: PumpMetricsHook) -> None:
        """Hold a pair until both hooks hold one, then release it."""
        hook(_hold(1))
        both_held.wait(timeout=5.0)
        hook(_hold(-1))

    workers = [
        threading.Thread(target=hold_and_release, args=(hook,)) for hook in hooks
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5.0)

    values = collector.gauge_values(RUST_PUMP_HELD_PAIRS)
    assert max(values) == pytest.approx(2.0), (
        f"both hooks' holds must reach the shared gauge, found {values}"
    )
    assert values[-1] == pytest.approx(0.0), (
        f"the shared gauge must return to zero, found {values}"
    )


class _SlotsGaugeCollector:
    """A gauge collector with no ``__weakref__`` slot."""

    __slots__ = ("gauges",)

    def __init__(self) -> None:
        """Start with no recorded gauge values."""
        self.gauges: list[float] = []

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Ignore counter increments."""
        del name, value, labels

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Ignore histogram observations."""
        del name, value, labels

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record the reported value."""
        del name, labels
        self.gauges.append(value)


def test_a_collector_without_weakref_support_shares_one_total() -> None:
    """A slotted collector still gives every hook the same running total."""
    collector = _SlotsGaugeCollector()
    first = PumpMetricsHook(typ.cast("typ.Any", collector))
    second = PumpMetricsHook(typ.cast("typ.Any", collector))

    first(_hold(1))
    second(_hold(1))
    first(_hold(-1))
    second(_hold(-1))

    assert collector.gauges == [1.0, 2.0, 1.0, 0.0], (
        "a collector without weakref support must still share its held count"
    )


def test_nested_registrations_count_one_hold_once() -> None:
    """One event delivered to both nested hooks moves the gauge once."""
    collector = RecordingCollector()

    with (
        observe_pump(PumpMetricsHook(collector)),
        observe_pump(PumpMetricsHook(collector)),
    ):
        _emit_pump_event(_hold(1))
        _emit_pump_event(_hold(-1))

    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 0.0], (
        "a nested registration must not count one held pair twice"
    )


def test_separate_contexts_report_their_own_holds() -> None:
    """Two registrations in private contexts hold two pairs at once."""
    collector = RecordingCollector()
    both_held = threading.Barrier(2)

    def hold_and_release(context: contextvars.Context) -> None:
        """Register in ``context``, then hold a pair until its peer does."""

        def work() -> None:
            with observe_pump(PumpMetricsHook(collector)):
                _emit_pump_event(_hold(1))
                both_held.wait(timeout=5.0)
                _emit_pump_event(_hold(-1))

        context.run(work)

    workers = [
        threading.Thread(target=hold_and_release, args=(contextvars.Context(),))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5.0)

    values = collector.gauge_values(RUST_PUMP_HELD_PAIRS)
    assert max(values) == pytest.approx(2.0), (
        f"each context must contribute its held pair, found {values}"
    )
    assert values[-1] == pytest.approx(0.0), (
        f"both contexts' releases must settle the gauge, found {values}"
    )


class HeldPairAccountingMachine(RuleBasedStateMachine):
    """Interleave holds across hooks that report to one collector.

    The gauge is one absolute value per collector, so every hook reporting to
    it must move a single count. The model records how many pairs each hook
    holds; after every rule the gauge must equal their total, and no published
    value may be negative.
    """

    _HOOKS = 3

    def __init__(self) -> None:
        """Start with one collector, three hooks, and an empty model."""
        super().__init__()
        self.collector = RecordingCollector()
        self.hooks = [PumpMetricsHook(self.collector) for _ in range(self._HOOKS)]
        self.held = [0] * self._HOOKS

    @rule(index=st.integers(min_value=0, max_value=_HOOKS - 1))
    def acquire(self, index: int) -> None:
        """Hold one pair through the hook at ``index``."""
        self.hooks[index](_hold(1))
        self.held[index] += 1

    @rule(index=st.integers(min_value=0, max_value=_HOOKS - 1))
    def release(self, index: int) -> None:
        """Release one pair through the hook at ``index`` if it holds one."""
        if self.held[index] == 0:
            return
        self.hooks[index](_hold(-1))
        self.held[index] -= 1

    @rule(
        index=st.integers(min_value=0, max_value=_HOOKS - 1),
        delta=st.sampled_from([0, 2, -2]),
    )
    def malformed_delta(self, index: int, delta: int) -> None:
        """Offer a delta no coordinator emits; the gauge must ignore it."""
        self.hooks[index](_hold(delta))

    @invariant()
    def gauge_matches_the_model(self) -> None:
        """Assert the shared gauge equals the model's total after every rule."""
        values = self.collector.gauge_values(RUST_PUMP_HELD_PAIRS)
        expected = float(sum(self.held))
        assert not values or values[-1] == pytest.approx(expected), (
            f"the gauge must report {expected} held pairs, found {values}"
        )
        assert all(value >= 0.0 for value in values), (
            f"the gauge must never report a negative count, found {values}"
        )


TestHeldPairAccountingMachine = HeldPairAccountingMachine.TestCase
TestHeldPairAccountingMachine.settings = settings(
    max_examples=40,
    stateful_step_count=20,
    deadline=None,
)


def test_the_reference_collector_reports_and_clears_gauges() -> None:
    """``InMemoryMetrics`` provides the optional capability."""
    metrics = InMemoryMetrics()

    assert isinstance(metrics, GaugeMetricsCollector), (
        "the reference collector must offer the gauge capability"
    )
    PumpMetricsHook(metrics)(_hold(1))
    assert metrics.gauges[RUST_PUMP_HELD_PAIRS] == pytest.approx(1.0)

    metrics.reset()
    assert not metrics.gauges, "reset must clear reported gauges"
