"""Counters for Rust-pump routing decisions, driven from the real pump paths.

A decline changes nothing a caller can see, so the only way to prove a counter
fires is to reach the seam that refuses. Every test here drives
``_pump_over_raw_fds`` or ``_try_rust_pump`` with the descriptor state that
provokes a real decline; none calls the recording helper by hand. Removing the
emission from the pump therefore fails these tests rather than leaving them
green against a stub.
"""

from __future__ import annotations

import asyncio
import threading
import typing as typ

import pytest

from cuprum import observe
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook
from cuprum.adapters.pump_metrics import (
    RUST_PUMP_DECLINED_TOTAL,
    RUST_PUMP_FAILED_AFTER_CANCEL_TOTAL,
    UNKNOWN_DECLINE_REASON,
    PumpMetricsHook,
    pump_metrics_hook,
)
from cuprum.context import current_context
from cuprum.events import ExecPhase
from cuprum.pump_events import PumpEvent, PumpPhase, RustPumpDeclineReason
from cuprum.pump_observation import current_pump_hooks, observe_pump
from cuprum.unittests._rust_pump_test_helpers import (
    DECLINE_PATHS,
    RecordingCollector,
    cancel_mid_transfer,
    hand_off_successfully,
    install_fake_pump,
    run_failing_pump_on_a_cancelled_hop,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Every value the ``reason`` label can take, derived from the two places that
# define them rather than restated here: a second copy would drift, and the
# drift would name the adapter rather than the call site that caused it.
_BOUNDED_REASONS = frozenset(
    {reason.value for reason in RustPumpDeclineReason} | {UNKNOWN_DECLINE_REASON},
)


@pytest.mark.parametrize(
    ("trigger", "expected_reason"),
    [(trigger, reason) for _id, trigger, reason in DECLINE_PATHS],
    ids=[path_id for path_id, _trigger, _reason in DECLINE_PATHS],
)
def test_each_real_decline_path_increments_once(
    trigger: cabc.Callable[[pytest.MonkeyPatch], None],
    expected_reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One decline, one increment, labelled with the seam that refused."""
    collector = RecordingCollector()
    with observe_pump(PumpMetricsHook(collector)):
        trigger(monkeypatch)

    assert len(collector.counters) == 1, (
        f"a {expected_reason!r} decline must increment exactly one counter, "
        f"found {collector.counters}"
    )
    name, value, labels = collector.counters[0]
    assert name == RUST_PUMP_DECLINED_TOTAL, (
        f"expected {RUST_PUMP_DECLINED_TOTAL!r}, found {name!r}"
    )
    assert value == 1.0, f"a decline counts as one, found {value}"
    assert labels == {"reason": expected_reason}, (
        f"expected the reason label alone, found {labels}"
    )


@pytest.mark.parametrize(
    ("trigger", "expected_reason"),
    [(trigger, reason) for _id, trigger, reason in DECLINE_PATHS],
    ids=[path_id for path_id, _trigger, _reason in DECLINE_PATHS],
)
def test_decline_labels_stay_inside_the_closed_reason_set(
    trigger: cabc.Callable[[pytest.MonkeyPatch], None],
    expected_reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``reason`` is labelled, and only with an enumerated value.

    Cardinality is the whole risk here. A descriptor number, an argument
    vector, or an exception message on a label would multiply the series count
    without bound, so the label set is pinned rather than merely reviewed.
    """
    del expected_reason
    collector = RecordingCollector()
    with observe_pump(pump_metrics_hook(collector)):
        trigger(monkeypatch)

    assert collector.counters, (
        "the decline must reach the collector, or the label assertions below "
        "inspect nothing and pass for it"
    )
    for name, _value, labels in collector.counters:
        assert set(labels) == {"reason"}, (
            f"{name!r} must carry the reason label alone, found {sorted(labels)}"
        )
        assert labels["reason"] in _BOUNDED_REASONS, (
            f"{name!r} carried an unbounded reason {labels['reason']!r}"
        )


def test_a_successful_hand_off_counts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hop the Rust pump accepts produces no pump counter at all.

    Without this control, a counter wired to fire on every hop would satisfy
    every decline assertion above while reporting a fast path that never
    declined as permanently degraded.
    """
    collector = RecordingCollector()
    with observe_pump(PumpMetricsHook(collector)):
        handled = hand_off_successfully(monkeypatch)

    assert handled is True, "the fixture must drive a hop the Rust pump handles"
    assert collector.counters == [], (
        f"a successful hand-off must record nothing, found {collector.counters}"
    )


def test_a_failure_recovered_after_cancellation_counts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation-masked pump failure increments one unlabelled counter."""
    collector = RecordingCollector()
    with observe_pump(PumpMetricsHook(collector)):
        run_failing_pump_on_a_cancelled_hop(monkeypatch)

    assert len(collector.counters) == 1, (
        "a pump failure recovered after cancellation must increment exactly "
        f"one counter, found {collector.counters}"
    )
    name, value, labels = collector.counters[0]
    assert name == RUST_PUMP_FAILED_AFTER_CANCEL_TOTAL, (
        f"expected {RUST_PUMP_FAILED_AFTER_CANCEL_TOTAL!r}, found {name!r}"
    )
    assert value == 1.0, f"a recovered failure counts as one, found {value}"
    assert labels == {}, (
        "this counter is deliberately unlabelled — an exception type would be "
        f"a series per failure mode — but it carried {labels}"
    )


def test_a_cancelled_hop_whose_pump_succeeded_counts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation alone is not a failure, so it records nothing.

    ``_report_pump_outcome_after_cancel`` runs on every cancelled hop. A
    counter placed before its outcome checks would turn ordinary cancellation
    into a reported pump failure.
    """
    release = threading.Event()
    worker_started = threading.Event()

    def succeeding_pump(reader_fd: int, writer_fd: int) -> int:
        """Return normally once the cancellation has been delivered."""
        del reader_fd, writer_fd
        worker_started.set()
        release.wait(timeout=5.0)
        return 0

    install_fake_pump(monkeypatch, succeeding_pump)
    collector = RecordingCollector()
    with observe_pump(PumpMetricsHook(collector)):
        asyncio.run(cancel_mid_transfer(worker_started, release))

    assert collector.counters == [], (
        f"a cancelled hop with a healthy pump records nothing, found "
        f"{collector.counters}"
    )


def test_declines_reach_no_collector_when_nobody_registers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller who never registers a pump hook sees the channel do nothing.

    A witness hook is registered alongside, because "nothing was recorded" is
    the same observation whether the channel skipped the unregistered hook or
    the hop never declined at all. The witness separates the two.
    """
    unregistered = RecordingCollector()
    witness = RecordingCollector()
    # The hook exists and wraps `unregistered`; it is simply never registered.
    unregistered_hook = PumpMetricsHook(unregistered)
    _id, trigger, _reason = DECLINE_PATHS[0]

    with observe_pump(PumpMetricsHook(witness)):
        assert unregistered_hook not in current_pump_hooks(), (
            "the unregistered hook must stay off the channel, or this test "
            f"proves nothing; found {current_pump_hooks()}"
        )
        trigger(monkeypatch)

    assert witness.counters, (
        "the hop must actually decline, or the emptiness assertion below holds "
        "even when the channel emits nothing at all"
    )
    assert unregistered.counters == [], (
        f"an unregistered collector must stay untouched, found {unregistered.counters}"
    )


def test_pump_phases_stay_out_of_the_exec_phase_contract() -> None:
    """No pump phase leaks into ``ExecPhase``.

    This is the constraint the whole design exists to satisfy. ``MetricsHook``
    matches ``ExecPhase`` exhaustively and raises on anything else, so a pump
    phase added to that literal would start raising inside consumers that were
    correct when they were written.
    """
    exec_phases = set(typ.get_args(ExecPhase.__value__))
    pump_phases = set(typ.get_args(PumpPhase.__value__))
    assert exec_phases == {
        "plan",
        "start",
        "stdout",
        "stderr",
        "exit",
        "stdin",
        "stdin_error",
    }, f"ExecPhase gained or lost a member: {sorted(exec_phases)}"
    assert not exec_phases & pump_phases, (
        f"pump phases must not appear in ExecPhase: {sorted(exec_phases & pump_phases)}"
    )


def test_an_already_registered_metrics_hook_is_untouched_by_a_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decline never reaches an ``ExecEvent`` consumer registered earlier.

    ``MetricsHook`` raises ``_UnhandledMetricsPhaseError`` on a phase it does
    not know. Routing pump events through the exec channel would therefore fail
    a pipeline whose owner had simply registered metrics, so the two channels
    are checked to stay disjoint at run time and not merely by type.
    """
    exec_metrics = InMemoryMetrics()
    pump_collector = RecordingCollector()
    exec_hook = MetricsHook(exec_metrics)

    with observe(exec_hook), observe_pump(PumpMetricsHook(pump_collector)):
        # Registration is asserted, not assumed: an exec hook that was never
        # installed could not be reached by a pump event either, which would
        # make the rest of this test pass for the wrong reason.
        assert current_context().observe_hooks == (exec_hook,), (
            "the exec-channel hook must actually be registered for this test "
            f"to mean anything, found {current_context().observe_hooks}"
        )
        for _id, trigger, _reason in DECLINE_PATHS:
            with monkeypatch.context() as patch:
                trigger(patch)

    assert exec_metrics.counters == {}, (
        f"a pump decline must not produce exec metrics, found {exec_metrics.counters}"
    )
    assert len(pump_collector.counters) == len(DECLINE_PATHS), (
        f"every decline must still be counted, found {pump_collector.counters}"
    )


def test_an_unknown_pump_phase_is_ignored_rather_than_raised_on() -> None:
    """A future phase must not raise inside an already-registered pump hook.

    The same fail-closed match that blocked this feature on the exec channel
    would recreate the problem here one release later.
    """
    collector = RecordingCollector()
    hook = PumpMetricsHook(collector)
    unknown = PumpEvent(phase=typ.cast("PumpPhase", "some_future_phase"))

    hook(unknown)

    assert collector.counters == [], (
        f"an unknown phase must record nothing, found {collector.counters}"
    )


def test_a_decline_without_a_reason_falls_back_to_a_bounded_label() -> None:
    """A reasonless decline still yields a label inside the closed set."""
    collector = RecordingCollector()
    PumpMetricsHook(collector)(PumpEvent(phase="declined"))

    _name, _value, labels = collector.counters[0]
    assert labels == {"reason": "unknown"}, (
        f"a missing reason must degrade to a fixed label, found {labels}"
    )
