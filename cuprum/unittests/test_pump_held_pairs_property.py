"""Generated delivery schedules for the shared held-pair gauge.

``cuprum_rust_pump_held_pairs`` counts held pairs, not hook deliveries. The
deterministic tests in ``test_pump_held_pairs_sharing`` pin one interleaving
each; this property generalizes them: whatever order concurrent emitters run
in, and whichever hooks of a shared collector each emission reaches, the gauge
must end at the number of emissions and return to zero once each is released.

Real threads race nondeterministically, which would make a schedule-dependent
test flaky in one direction and unconvincing in the other. The deliveries
therefore still run on real threads — the counter's memory is thread-local, so
it has to — but a generated schedule decides exactly when each worker runs the
next step. Every generated interleaving is replayed deterministically, so a
failure names a real defect rather than a lost race.
"""

from __future__ import annotations

import threading
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum.adapters.pump_metrics import (
    RUST_PUMP_HELD_PAIRS,
    PumpMetricsHook,
)
from cuprum.pump_events import PumpEvent
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from hypothesis.strategies import DrawFn

    type _Step = cabc.Callable[[], None]
    type _Plan = tuple[list[tuple[int, frozenset[int]]], list[tuple[int, int]]]

# One emission names the worker that delivers it and the hooks it reaches. An
# emission always reaches at least one hook and is delivered inline on the
# thread that emitted it, so a schedule may not split one emission across
# threads: that would describe two emissions sharing one event object, which
# the emitter never produces.
_EMISSION = st.tuples(
    st.integers(min_value=0, max_value=2),
    st.frozensets(st.integers(min_value=0, max_value=1), min_size=1),
)

_HANG_S = 10.0


class _Turnstile:
    """Run one worker thread's steps in an order decided by another thread.

    The worker blocks until the driving thread hands it a step, so the whole
    schedule is replayed in the order the driving thread chooses. A step that
    raises is kept and re-raised on the driving thread, because an exception
    escaping a worker thread would otherwise only print.
    """

    def __init__(self) -> None:
        """Start with no step to run and no failure recorded."""
        self._go = threading.Event()
        self._done = threading.Event()
        self._step: _Step | None = None
        self._failure: BaseException | None = None
        self.thread = threading.Thread(target=self._serve)

    def run(self, step: _Step) -> None:
        """Run ``step`` on the worker and wait for it to finish or fail."""
        self._step = step
        self._go.set()
        assert self._done.wait(timeout=_HANG_S), "the worker must finish its step"
        self._done.clear()
        failure, self._failure = self._failure, None
        if failure is not None:
            raise failure

    def stop(self) -> None:
        """Ask the worker to exit and wait for it."""
        self._step = None
        self._go.set()
        self.thread.join(timeout=_HANG_S)
        assert not self.thread.is_alive(), "the worker must exit when released"

    def _serve(self) -> None:
        """Run each handed step until the driving thread stops the worker."""
        while True:
            self._go.wait(timeout=_HANG_S)
            self._go.clear()
            step, self._step = self._step, None
            if step is None:
                return
            try:
                step()
            except BaseException as exc:  # ruff: ignore[blind-except] - re-raised by ``run``
                self._failure = exc
            self._done.set()


@st.composite
def _schedules(draw: DrawFn) -> _Plan:
    """Draw emissions and one legal interleaving of their deliveries.

    The order is a merge of each worker's own delivery stream, not an
    arbitrary permutation: a worker delivers its emissions in program order,
    so it can never start one emission before finishing the previous one.
    Another worker's deliveries may interleave anywhere between them, which is
    what the counter's per-thread memory has to tolerate.

    Returns
    -------
    _Plan
        The emissions and the order in which their deliveries run.
    """
    emissions = draw(st.lists(_EMISSION, min_size=1, max_size=4))
    streams: dict[int, list[tuple[int, int]]] = {}
    for index, (worker, reached) in enumerate(emissions):
        streams.setdefault(worker, []).extend((index, hook) for hook in sorted(reached))

    order: list[tuple[int, int]] = []
    remaining = {worker: len(stream) for worker, stream in streams.items()}
    while any(remaining.values()):
        worker = draw(
            st.sampled_from([name for name, left in remaining.items() if left])
        )
        order.append(streams[worker][len(streams[worker]) - remaining[worker]])
        remaining[worker] -= 1
    return emissions, order


def _deliver(
    plan: _Plan,
    events: cabc.Sequence[PumpEvent],
    hooks: cabc.Sequence[PumpMetricsHook],
) -> None:
    """Replay ``plan`` on one worker thread per emitter and join them."""
    emissions, order = plan
    workers = sorted({worker for worker, _reached in emissions})
    turnstiles = {worker: _Turnstile() for worker in workers}
    for turnstile in turnstiles.values():
        turnstile.thread.start()
    try:
        for index, hook in order:
            turnstiles[emissions[index][0]].run(
                lambda event=events[index], hook=hook: hooks[hook](event)
            )
    finally:
        for turnstile in turnstiles.values():
            turnstile.stop()


@settings(max_examples=25)
@given(plan=_schedules())
def test_a_generated_schedule_moves_the_gauge_once_per_emission(plan: _Plan) -> None:
    """A replayed interleaving leaves exactly one count per emission.

    Each emission is a distinct event object delivered on its own thread, so
    the gauge must end at the number of emissions however the deliveries are
    ordered, and it must return to zero once every hold is released the same
    way.
    """
    emissions, _order = plan
    collector = RecordingCollector()
    hooks = [PumpMetricsHook(collector), PumpMetricsHook(collector)]
    holds = [PumpEvent(phase="handoff_hold", held_delta=1) for _ in emissions]
    releases = [PumpEvent(phase="handoff_hold", held_delta=-1) for _ in emissions]

    _deliver(plan, holds, hooks)
    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS)[-1] == pytest.approx(
        float(len(emissions))
    ), "each emission must move the shared gauge exactly once"

    _deliver(plan, releases, hooks)
    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS)[-1] == pytest.approx(0.0), (
        "releasing every hold must return the gauge to zero"
    )
