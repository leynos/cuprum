"""Generated delivery schedules for the shared held-pair gauge.

``cuprum_rust_pump_held_pairs`` counts held pairs, not hook deliveries. The
deterministic tests in ``test_pump_held_pairs_sharing`` pin one interleaving
each; this property generalizes them: whatever order concurrent emitters run
in, whichever of the two metrics hooks a nested emission reaches before the
outer one finishes, and wherever one delivery is interrupted between its hooks
by another thread's complete emission, the gauge must end at the number of
emissions and return to zero once each is released.

Real threads race nondeterministically, which would make a schedule-dependent
test flaky in one direction and unconvincing in the other. The emissions
therefore still run on real threads — the counter's claims are per thread, so
they have to — but a generated schedule decides exactly when each worker
starts its next emission. Every generated interleaving is replayed
deterministically, so a failure names a real defect rather than a lost race.
"""

from __future__ import annotations

import contextvars
import dataclasses as dc
import threading
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum.adapters.pump_metrics import (
    RUST_PUMP_HELD_PAIRS,
    PumpMetricsHook,
)
from cuprum.pump_observation import _emit_rust_pump_hold, observe_pump
from cuprum.unittests._rust_pump_test_helpers import (
    NestedHoldEmitter,
    PausingHook,
    RecordingCollector,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from hypothesis.strategies import DrawFn

    type _Step = cabc.Callable[[], None]
    # One emission names the worker that delivers it, whether the hook inside
    # the delivery emits a nested hold, and whether the delivery pauses after
    # its first hook so another worker can complete an emission in between.
    type _Emission = tuple[int, bool, bool]
    type _Plan = tuple[list[_Emission], list[int]]

# At most three workers and four emissions keep every replayed schedule short
# while still interleaving two workers, nesting inside a delivery, and
# interrupting one delivery between its hooks.
_EMISSION = st.tuples(
    st.integers(min_value=0, max_value=2),
    st.booleans(),
    st.booleans(),
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

    def start(self, step: _Step) -> None:
        """Hand ``step`` to the worker without waiting for it to finish."""
        self._step = step
        self._go.set()

    def settle(self) -> None:
        """Wait for the handed step, re-raising any failure it recorded."""
        assert self._done.wait(timeout=_HANG_S), "the worker must finish its step"
        self._done.clear()
        failure, self._failure = self._failure, None
        if failure is not None:
            raise failure

    def run(self, step: _Step) -> None:
        """Run ``step`` on the worker and wait for it to finish or fail."""
        self.start(step)
        self.settle()

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
            except BaseException as exc:  # ruff: ignore[blind-except] - re-raised by ``settle``
                self._failure = exc
            self._done.set()


@st.composite
def _schedules(draw: DrawFn) -> _Plan:
    """Draw emissions and one legal interleaving of them.

    The order is a merge of each worker's own emission stream, not an
    arbitrary permutation: a worker emits in program order, so it can never
    start its next emission before the previous one has been delivered. Another
    worker's emission may interleave anywhere between them, which is what the
    counter's per-thread claims have to tolerate.

    Returns
    -------
    _Plan
        The emissions and the order in which the workers run them.
    """
    emissions = draw(st.lists(_EMISSION, min_size=1, max_size=4))
    streams: dict[int, list[int]] = {}
    for index, (worker, _nests, _pauses) in enumerate(emissions):
        streams.setdefault(worker, []).append(index)

    order: list[int] = []
    remaining = {worker: len(stream) for worker, stream in streams.items()}
    while any(remaining.values()):
        worker = draw(
            st.sampled_from([name for name, left in remaining.items() if left])
        )
        order.append(streams[worker][len(streams[worker]) - remaining[worker]])
        remaining[worker] -= 1
    return emissions, order


@dc.dataclass(slots=True)
class _Replay:
    """One phase's emissions: the delta, the hooks, and the context to enter."""

    context: contextvars.Context
    delta: int
    nested: NestedHoldEmitter
    gate: PausingHook

    def step(self, emission: _Emission, *, pauses: bool) -> _Step:
        """Build the worker step for ``emission``, armed on the worker thread.

        Arming happens here rather than in the driving thread so one worker's
        nesting decision and pause cannot leak into a delivery that is paused
        on another thread. Each step enters its own copy of the context: one
        context object cannot be entered twice at once, and a paused delivery
        keeps its copy entered while another worker completes an emission.
        """
        nests = emission[1]

        def emit() -> None:
            self.nested.arm(nest=nests)
            if pauses:
                self.gate.arm()
            self.context.copy().run(_emit_rust_pump_hold, self.delta)

        return emit


class _Schedule:
    """Replay one drawn plan as real emissions, one worker thread per emitter."""

    def __init__(self, plan: _Plan, replay: _Replay) -> None:
        """Start each worker's thread; nothing runs until :meth:`deliver`."""
        self._plan = plan
        self._replay = replay
        emissions, _order = plan
        workers = sorted({worker for worker, _nests, _pauses in emissions})
        self._turnstiles = {worker: _Turnstile() for worker in workers}

    def deliver(self) -> None:
        """Run every emission in the drawn order, interleaving where drawn."""
        for turnstile in self._turnstiles.values():
            turnstile.thread.start()
        try:
            _emissions, order = self._plan
            index = 0
            while index < len(order):
                index = self._advance(index)
        finally:
            # A failed assertion must not leave a delivery blocked behind the gate.
            self._replay.gate.release()
            for turnstile in self._turnstiles.values():
                turnstile.stop()

    def _advance(self, index: int) -> int:
        """Run the emission at ``index``, interleaving inside it when it pauses.

        Returns
        -------
        int
            The index of the next emission the replay loop must run.
        """
        emissions, order = self._plan
        emission = emissions[order[index]]
        worker, _nests, pauses = emission
        turnstile = self._turnstiles[worker]
        turnstile.start(self._replay.step(emission, pauses=pauses))
        if not pauses:
            turnstile.settle()
            return index + 1

        assert self._replay.gate.wait_paused(_HANG_S), (
            "the paused delivery must reach the gate"
        )
        index = self._run_others(index + 1, worker)
        self._replay.gate.release()
        turnstile.settle()
        return index

    def _run_others(self, index: int, paused_worker: int) -> int:
        """Complete other workers' emissions while one delivery waits mid-flight.

        Only an emission another worker may run is taken: the paused worker's
        own stream stays in order, and a second pausing emission is left for
        :meth:`_advance`, because one gate can hold only one delivery at a time.

        Returns
        -------
        int
            The index of the next emission :meth:`deliver` must run.
        """
        emissions, order = self._plan
        while index < len(order):
            emission = emissions[order[index]]
            worker, _nests, pauses = emission
            if worker == paused_worker or pauses:
                return index
            self._turnstiles[worker].run(self._replay.step(emission, pauses=False))
            index += 1
        return index


def _deliver(plan: _Plan, replay: _Replay) -> None:
    """Replay ``plan`` through ``replay``'s hooks and context."""
    _Schedule(plan, replay).deliver()


@settings(max_examples=25)
@given(plan=_schedules())
def test_a_generated_schedule_moves_the_gauge_once_per_emission(plan: _Plan) -> None:
    """A replayed interleaving leaves exactly one count per emission.

    Every emission is its own event, so the gauge must end at the number of
    emissions — including the nested ones a hook adds inside a delivery — and
    it must return to zero once each hold is released the same way.
    """
    emissions, _order = plan
    nested_count = sum(1 for _worker, nests, _pauses in emissions if nests)
    total = len(emissions) + nested_count
    collector = RecordingCollector()
    nested = NestedHoldEmitter()
    gate = PausingHook()

    with (
        observe_pump(PumpMetricsHook(collector)),
        observe_pump(gate),
        observe_pump(nested),
        observe_pump(PumpMetricsHook(collector)),
    ):
        replay = _Replay(
            context=contextvars.copy_context(),
            delta=1,
            nested=nested,
            gate=gate,
        )
        _deliver(plan, replay)
        assert collector.gauge_values(RUST_PUMP_HELD_PAIRS)[-1] == pytest.approx(
            float(total)
        ), "each emission must move the shared gauge exactly once"

        _deliver(plan, dc.replace(replay, delta=-1))
        assert collector.gauge_values(RUST_PUMP_HELD_PAIRS)[-1] == pytest.approx(0.0), (
            "releasing every hold must return the gauge to zero"
        )
