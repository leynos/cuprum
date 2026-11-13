"""Generated schedules in which several hand-offs hold pairs at once.

The sibling settlement module finishes each hand-off before starting the next,
so it proves every path balances the one hold it took. This module keeps
several real hand-offs suspended in their buffered drain simultaneously and
settles them in a generated order of successes, drain failures, and
cancellations. After every transition the shared gauge must report the pairs
still held, and every descriptor an act acquired must be closed exactly once:
a leak leaves a descriptor open, and a second close can release a number the
next act already owns.

Every act drives the real coordinator over a private pipe. Only the buffered
drain, the native worker, and the descriptor-close helpers are doubled.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import threading
import typing as typ

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from cuprum import (
    _pipeline_stream_native_cleanup,
    _pipeline_stream_native_completion,
    _pipeline_streams,
)
from cuprum.adapters.pump_metrics import RUST_PUMP_HELD_PAIRS, PumpMetricsHook
from cuprum.pump_events import PumpEvent, RustPumpHandoffOutcome
from cuprum.pump_observation import observe_pump
from cuprum.unittests._pump_stream_dispatch_support import clear_backend_caches
from cuprum.unittests._rust_pump_test_helpers import (
    RecordingCollector,
    allow_pause,
    install_fake_pump,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_native_pump_types import (
        _NativePumpFds,
        _RustPumpState,
    )
    from cuprum._pipeline_stream_fds import _NativePumpWorkerFds

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")

# Three held pairs are enough to interleave two settlements around a third
# while keeping every generated example short.
_MAX_HELD = 3


class _CloseAudit:
    """Count the descriptor closes the pump performs, by number.

    The native worker closes its adopted writer on a worker thread while the
    coordinator closes the rest on the loop thread, so the count is guarded.
    """

    def __init__(self) -> None:
        """Start with no recorded closes."""
        self._lock = threading.Lock()
        self._counts: collections.Counter[int] = collections.Counter()

    def record(self, fd: int) -> None:
        """Count one close of ``fd``."""
        with self._lock:
            self._counts[fd] += 1

    def count(self, fd: int) -> int:
        """Return how many times ``fd`` was closed."""
        with self._lock:
            return self._counts.get(fd, 0)

    def closed(self) -> set[int]:
        """Return every descriptor number that was closed."""
        with self._lock:
            return set(self._counts)

    def forget(self, fds: cabc.Iterable[int]) -> None:
        """Retire ``fds`` so a reused number is counted from zero again."""
        with self._lock:
            for fd in fds:
                self._counts.pop(fd, None)


def _counted_close(
    real: cabc.Callable[[int], None],
    audit: _CloseAudit,
) -> cabc.Callable[[int], None]:
    """Wrap a close helper so every call is counted before it runs."""

    def closing(fd: int) -> None:
        """Count one close, then perform it."""
        audit.record(fd)
        real(fd)

    return closing


class _HeldHandoff:
    """One hand-off parked inside its buffered drain.

    ``marker`` identifies the act's drain without needing a real stream: the
    patched drain receives it as the reader and looks the act up by identity.
    """

    def __init__(
        self,
        reader_fd: int,
        writer_fd: int,
        *,
        drain_fails: bool,
    ) -> None:
        """Record the transport pair, the drain shape, and its gates."""
        self.reader_fd = reader_fd
        self.writer_fd = writer_fd
        self.drain_fails = drain_fails
        self.marker = object()
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.task: asyncio.Task[bool] | None = None
        self.worker_fds: tuple[int, int] | None = None
        self.native_fds: tuple[int, int] | None = None
        self.outcomes: list[RustPumpHandoffOutcome] = []
        self.audited = False

    @property
    def owned_fds(self) -> tuple[int, ...]:
        """Every descriptor this act acquired and must close."""
        fds = list(self.worker_fds or ())
        fds.extend(self.native_fds or ())
        return tuple(fds)


class ConcurrentHandoffMachine(RuleBasedStateMachine):
    """Hold several real hand-offs at once and settle them in any order.

    Each rule starts one hand-off or settles one already held. The invariant
    after every rule requires the shared gauge to report exactly the acts still
    parked, and every settlement audits its act's descriptors.
    """

    def __init__(self) -> None:
        """Open one collector, one loop, and the hand-off seams."""
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.collector = RecordingCollector()
        self.audit = _CloseAudit()
        self.acts: list[_HeldHandoff] = []
        self.pending: list[_HeldHandoff] = []
        self.held = 0
        self._by_marker: dict[int, _HeldHandoff] = {}
        self._pending_opens: list[tuple[int, int]] = []
        self._settling: _HeldHandoff | None = None
        self._patches = contextlib.ExitStack()
        self._real_open = _pipeline_stream_native_cleanup._open_native_pump_worker_fds
        self._real_duplicate = (
            _pipeline_stream_native_cleanup._duplicate_native_pump_fds
        )
        self._install()

    def _install(self) -> None:
        """Apply the seams every act shares for the machine's lifetime."""
        patch = self._patches.enter_context(pytest.MonkeyPatch.context())
        allow_pause(patch, may_hand_off=True)
        patch.setattr(_pipeline_streams, "_drain_reader_buffer", self._drain)
        patch.setattr(
            _pipeline_stream_native_cleanup,
            "_open_native_pump_worker_fds",
            self._record_open,
        )
        patch.setattr(
            _pipeline_stream_native_cleanup,
            "_duplicate_native_pump_fds",
            self._record_duplicate,
        )
        for module, name in (
            (_pipeline_stream_native_cleanup, "_close_rust_reader_fd"),
            (_pipeline_stream_native_cleanup, "_close_rust_writer_fd"),
            (_pipeline_stream_native_cleanup, "_close_native_pump_worker_fd"),
            (_pipeline_stream_native_completion, "_close_rust_reader_fd"),
            (_pipeline_stream_native_completion, "_close_rust_state_fd"),
        ):
            real = getattr(module, name)
            patch.setattr(module, name, _counted_close(real, self.audit))
        install_fake_pump(patch, self._native_pump)

    async def _drain(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Announce the drain, wait to be released, then fail if asked to."""
        del writer
        act = self._by_marker[id(reader)]
        act.started.set()
        await act.gate.wait()
        if act.drain_fails:
            msg = "buffered drain failed"
            raise RuntimeError(msg)

    def _record_open(self, **kwargs: int) -> _NativePumpWorkerFds | None:
        """Record the worker pair the real acquisition opened."""
        opened = self._real_open(**kwargs)
        if opened is not None:
            self._pending_opens.append((opened.reader_fd, opened.writer_fd))
        return opened

    def _record_duplicate(self, state: _RustPumpState) -> _NativePumpFds:
        """Record the callback-owned duplicates opened for a submission."""
        native_fds = self._real_duplicate(state)
        if self._settling is not None:
            self._settling.native_fds = (
                native_fds.reader_fd,
                native_fds.writer_fd,
            )
        return native_fds

    def _native_pump(self, reader_fd: int, writer_fd: int) -> int:
        """Stand in for the native worker, which adopts and closes its writer."""
        del reader_fd
        self.audit.record(writer_fd)
        with contextlib.suppress(OSError):
            os.close(writer_fd)
        return 0

    def _record_outcome(self, event: PumpEvent) -> None:
        """Attribute a published outcome to the act being settled."""
        if event.phase != "handoff":
            return
        if not isinstance(event.outcome, RustPumpHandoffOutcome):
            return
        if self._settling is not None:
            self._settling.outcomes.append(event.outcome)

    def _run(self, operation: cabc.Coroutine[typ.Any, typ.Any, None]) -> None:
        """Run one hand-off transition with both observation hooks registered."""
        with (
            observe_pump(PumpMetricsHook(self.collector)),
            observe_pump(self._record_outcome),
        ):
            self.loop.run_until_complete(operation)

    @staticmethod
    def _task_of(act: _HeldHandoff) -> asyncio.Task[bool]:
        """Return the act's task, which every settlement path requires."""
        task = act.task
        assert task is not None, "an act is settled only after it was started"
        return task

    async def _drive(self, act: _HeldHandoff) -> bool:
        """Drive the real coordinator over the descriptors the act owns."""
        return await _pipeline_streams._pump_over_raw_fds(
            reader=typ.cast("asyncio.StreamReader", act.marker),
            writer=None,
            handoff=_pipeline_stream_native_cleanup._RustPumpHandoff(
                reader_fd=act.reader_fd,
                writer_fd=act.writer_fd,
                cleanup_grace_s=0.5,
            ),
        )

    async def _start(self, act: _HeldHandoff) -> None:
        """Start the act and wait until its drain holds the pair."""
        act.task = asyncio.create_task(self._drive(act))
        await act.started.wait()

    async def _release(self, act: _HeldHandoff) -> None:
        """Let the drain finish, expecting the shape the act carries."""
        task = self._task_of(act)
        act.gate.set()
        if not act.drain_fails:
            assert await task, "the native pump must carry a completed hop"
            return
        with pytest.raises(RuntimeError, match="buffered drain failed"):
            await task

    async def _cancel(self, act: _HeldHandoff) -> None:
        """Cancel the act while its drain is parked."""
        task = self._task_of(act)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def _cancel_quietly(self, act: _HeldHandoff) -> None:
        """Cancel a still-parked act without demanding an exception shape."""
        task = self._task_of(act)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, RuntimeError):
            await task

    def _published(self) -> list[float]:
        """Return the shared gauge's publications in order."""
        return self.collector.gauge_values(RUST_PUMP_HELD_PAIRS)

    def _assert_one_transition(self, before: int) -> None:
        """Assert the transition published exactly one balanced gauge value."""
        values = self._published()
        assert len(values) == before + 1, (
            f"one transition must publish one gauge value, found {values[before:]}"
        )
        assert values[-1] == pytest.approx(float(self.held)), (
            f"the gauge must report {self.held} held pairs, found {values[-1]}"
        )

    def _begin(self, *, drain_fails: bool) -> None:
        """Start one hand-off and leave it parked in its drain."""
        reader_fd, writer_fd = os.pipe()
        act = _HeldHandoff(reader_fd, writer_fd, drain_fails=drain_fails)
        self.acts.append(act)
        self._by_marker[id(act.marker)] = act
        before = len(self._published())
        self._run(self._start(act))
        assert len(self._pending_opens) == 1, (
            f"starting one act must open one worker pair, found {self._pending_opens}"
        )
        act.worker_fds = self._pending_opens.pop()
        for fd in act.worker_fds:
            os.fstat(fd)
        self.pending.append(act)
        self.held += 1
        self._assert_one_transition(before)

    def _settle(self, act: _HeldHandoff, *, cancel: bool) -> None:
        """Settle one parked act and audit what it owed."""
        before = len(self._published())
        if cancel:
            expected = [RustPumpHandoffOutcome.DRAIN_CANCELLED]
        elif act.drain_fails:
            expected = [RustPumpHandoffOutcome.DRAIN_FAILED]
        else:
            expected = [RustPumpHandoffOutcome.SUBMITTED]
        self.pending.remove(act)
        self._by_marker.pop(id(act.marker), None)
        self._settling = act
        try:
            self._run(self._cancel(act) if cancel else self._release(act))
        finally:
            self._settling = None
        self.held -= 1
        self._assert_one_transition(before)
        self._audit(act, expected)

    def _audit(
        self,
        act: _HeldHandoff,
        expected: list[RustPumpHandoffOutcome],
    ) -> None:
        """Assert the act published its outcome and closed its pair once each."""
        assert act.outcomes == expected, (
            f"the act must publish {expected}, found {act.outcomes}"
        )
        owned = act.owned_fds
        assert owned, "a settled act must have recorded the descriptors it owned"
        counts = {fd: self.audit.count(fd) for fd in owned}
        assert all(count == 1 for count in counts.values()), (
            f"every descriptor must be closed exactly once, found {counts}"
        )
        assert self.audit.closed() == set(owned), (
            f"settlement must close only its own descriptors, "
            f"found {self.audit.closed()} for {set(owned)}"
        )
        for fd in owned:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)
        self.audit.forget(owned)
        act.audited = True

    @rule(drain_fails=st.booleans())
    def begin_handoff(self, *, drain_fails: bool) -> None:
        """Park one more real hand-off inside its buffered drain."""
        if len(self.pending) >= _MAX_HELD:
            return
        self._begin(drain_fails=drain_fails)

    @rule(
        index=st.integers(min_value=0, max_value=_MAX_HELD - 1),
        cancel=st.booleans(),
    )
    def settle_handoff(self, index: int, *, cancel: bool) -> None:
        """Settle one parked hand-off, leaving the others still held."""
        if index >= len(self.pending):
            return
        self._settle(self.pending[index], cancel=cancel)

    @invariant()
    def the_gauge_reports_the_pairs_still_held(self) -> None:
        """Assert every held pair is counted once and no release goes below zero."""
        values = self._published()
        assert not values or values[-1] == pytest.approx(float(self.held)), (
            f"the gauge must report {self.held} held pairs, found {values}"
        )
        assert all(value >= 0.0 for value in values), (
            f"a release must never drive the gauge negative, found {values}"
        )

    def teardown(self) -> None:
        """Settle anything still parked, then release the act-owned resources."""
        try:
            self._settle_unfinished_acts()
        finally:
            self._patches.close()
            self._close_act_descriptors()
            self.loop.close()

    def _settle_unfinished_acts(self) -> None:
        """Cancel and audit every act an example left parked."""
        for act in self.acts:
            if act.task is None or act.audited:
                continue
            if not act.task.done():
                self._settling = act
                try:
                    self._run(self._cancel_quietly(act))
                finally:
                    self._settling = None
            if act.worker_fds is not None:
                self._audit(act, self._derived_outcomes(act))

    def _close_act_descriptors(self) -> None:
        """Close the transport pairs and any acquisition the act never took."""
        leftovers = [fd for pair in self._pending_opens for fd in pair]
        leftovers.extend(
            fd for act in self.acts for fd in (act.reader_fd, act.writer_fd)
        )
        for fd in leftovers:
            with contextlib.suppress(OSError):
                os.close(fd)

    @staticmethod
    def _derived_outcomes(act: _HeldHandoff) -> list[RustPumpHandoffOutcome]:
        """Derive what an unfinished act owed from how its task really ended."""
        task = act.task
        if task is None:
            # An act that never started published no hand-off outcome.
            return []
        if task.cancelled():
            return [RustPumpHandoffOutcome.DRAIN_CANCELLED]
        if task.exception() is not None:
            return [RustPumpHandoffOutcome.DRAIN_FAILED]
        return [RustPumpHandoffOutcome.SUBMITTED]


TestConcurrentHandoffMachine = ConcurrentHandoffMachine.TestCase
TestConcurrentHandoffMachine.settings = settings(
    max_examples=20,
    stateful_step_count=15,
    deadline=None,
)


def test_three_held_handoffs_settle_in_any_order() -> None:
    """Anchor the generated schedules to three pairs held at once.

    The property above decides for itself how many hand-offs overlap; this
    example pins the arrangement the generated schedules exist for, so a
    shrinking or strategy change cannot quietly make the overlap unreachable.
    """
    machine = ConcurrentHandoffMachine()
    try:
        machine._begin(drain_fails=False)
        machine._begin(drain_fails=True)
        machine._begin(drain_fails=False)
        assert machine._published()[-1] == pytest.approx(3.0), (
            "three parked hand-offs must all be counted as held"
        )
        first, failing, last = machine.pending

        machine._settle(failing, cancel=False)
        assert machine.held == 2, "a failed drain must release only its own pair"

        machine._settle(last, cancel=True)
        assert machine.held == 1, "a cancellation must release only its own pair"

        for fd in first.worker_fds or ():
            os.fstat(fd)
        machine._settle(first, cancel=False)
        assert machine.held == 0, "the last success must clear the gauge"
        assert machine._published()[-1] == pytest.approx(0.0), (
            f"the shared gauge must return to zero, found {machine._published()}"
        )
    finally:
        machine.teardown()
