"""Model generated delivery, sink-failure, and close interleavings."""

from __future__ import annotations

import io
import threading
import time

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from cuprum._bounded_delivery import _DeliveryPayload, _DeliveryWorker
from cuprum._stream_drain_state import _RelayDiagnostics
from cuprum.echo_events import EchoStream


class _SteppedSink(io.StringIO):
    """Hold each worker write until the model completes or fails it."""

    def __init__(self) -> None:
        """Create a sink with one release gate for every write attempt."""
        super().__init__()
        self._condition = threading.Condition()
        self._started: list[str] = []
        self._completed: list[str] = []
        self._releases: list[threading.Event] = []
        self._failed_writes: set[int] = set()
        self._release_future_writes = False

    def write(self, value: str, /) -> int:
        """Wait for the model to finish this write, then record success."""
        with self._condition:
            index = len(self._started)
            self._started.append(value)
            release = threading.Event()
            self._releases.append(release)
            release_immediately = self._release_future_writes
            self._condition.notify_all()
        if release_immediately:
            release.set()
        if not release.wait(timeout=3.0):
            msg = "state machine did not release a sink write"
            raise RuntimeError(msg)
        with self._condition:
            if index in self._failed_writes:
                msg = "selected state-machine sink failure"
                raise OSError(msg)
            self._completed.append(value)
        return super().write(value)

    def flush(self) -> None:
        """Accept each flush after its associated write has returned."""

    def wait_for_write(self, index: int) -> str:
        """Wait until the worker has started write ``index``."""
        with self._condition:
            started = self._condition.wait_for(
                lambda: len(self._started) > index,
                timeout=1.0,
            )
            assert started, f"worker must start expected write {index}"
            return self._started[index]

    def release(self, index: int, *, fail: bool = False) -> None:
        """Complete one blocked write, optionally selecting its failure."""
        with self._condition:
            if fail:
                self._failed_writes.add(index)
            release = self._releases[index]
        release.set()

    def release_all(self) -> None:
        """Release every write so state-machine cleanup cannot leak a thread."""
        with self._condition:
            self._release_future_writes = True
            releases = tuple(self._releases)
        for release in releases:
            release.set()

    @property
    def started(self) -> tuple[str, ...]:
        """Write attempts in worker order."""
        with self._condition:
            return tuple(self._started)

    @property
    def completed(self) -> tuple[str, ...]:
        """Writes whose sink calls completed successfully."""
        with self._condition:
            return tuple(self._completed)


type _Item = tuple[EchoStream, str, int]


class _DeliveryWorkerMachine(RuleBasedStateMachine):
    """Compare the worker's live transitions with a byte-accounting model."""

    def __init__(self) -> None:
        """Leave worker setup to the generated initialization rule."""
        super().__init__()
        self._started = False
        self._closed = False
        self._failed = False
        self._capacity = 0
        self._sink: _SteppedSink | None = None
        self._worker: _DeliveryWorker | None = None
        self._diagnostics: dict[EchoStream, _RelayDiagnostics] = {}
        self._active: _Item | None = None
        self._active_index = 0
        self._next_write_index = 0
        self._queued: list[_Item] = []
        self._attempted: list[str] = []
        self._completed: list[str] = []
        self._failures: list[EchoStream] = []
        self._undelivered: dict[EchoStream, int] = {}
        self._next_value = 0

    @initialize(data=st.data())
    def start_worker(self, data: st.DataObject) -> None:
        """Start with a blocked active write and a generated queue capacity."""
        self._capacity = data.draw(st.integers(min_value=1, max_value=16))
        seed_bytes = data.draw(
            st.integers(min_value=1, max_value=self._capacity),
        )
        self._sink = _SteppedSink()
        self._worker = _DeliveryWorker(
            destinations={
                EchoStream.STDOUT: self._sink,
                EchoStream.STDERR: self._sink,
            },
            max_queued_bytes=self._capacity,
        )
        self._diagnostics = {stream: _RelayDiagnostics() for stream in EchoStream}
        seed = self._new_item(EchoStream.STDOUT, seed_bytes)
        assert (
            self._worker.enqueue(
                seed[0],
                self._payload(seed),
                flush=True,
                diagnostics=self._diagnostics[seed[0]],
            )
            == 0
        ), "the first payload must enter an empty queue"
        self._active = seed
        self._active_index = 0
        self._next_write_index = 1
        self._attempted.append(self._sink.wait_for_write(0))
        self._started = True

    def _new_item(self, stream: EchoStream, source_bytes: int) -> _Item:
        """Create a unique payload with the requested source-byte weight."""
        value = f"{self._next_value}:" + ("x" * source_bytes)
        self._next_value += 1
        return stream, value, source_bytes

    @staticmethod
    def _payload(item: _Item) -> _DeliveryPayload:
        """Convert one modelled item to the worker's prepared payload."""
        return _DeliveryPayload(value=item[1], source_bytes=item[2])

    def _current_worker(self) -> _DeliveryWorker:
        """Return the worker after generated initialization."""
        worker = self._worker
        assert worker is not None, "initialization must build the worker"
        return worker

    def _current_sink(self) -> _SteppedSink:
        """Return the sink after generated initialization."""
        sink = self._sink
        assert sink is not None, "initialization must build the controlled sink"
        return sink

    def _add_undelivered(self, item: _Item) -> None:
        """Group loss totals by the worker's stream diagnostics target."""
        stream, _value, source_bytes = item
        self._undelivered[stream] = self._undelivered.get(stream, 0) + source_bytes

    def _wait_until_idle(self) -> None:
        """Wait for the worker to clear its active slot after a write."""
        worker = self._current_worker()
        deadline = time.monotonic() + 1.0
        with worker._condition:
            while worker._active_item is not None and time.monotonic() < deadline:
                worker._condition.wait(timeout=0.01)
            assert worker._active_item is None, (
                "a released final write must leave the worker idle"
            )

    def _assert_accounting(self) -> None:
        """Check byte bounds, delivery order, failures, and grouped losses."""
        worker = self._current_worker()
        sink = self._current_sink()
        queued_bytes = sum(item[2] for item in self._queued)
        assert worker.queued_bytes == queued_bytes, (
            f"queued-byte model mismatch: expected={queued_bytes}, "
            f"actual={worker.queued_bytes}"
        )
        assert queued_bytes <= self._capacity, (
            f"queue exceeded capacity: queued={queued_bytes}, capacity={self._capacity}"
        )
        assert sink.started == tuple(self._attempted), (
            f"write attempt order differs from model: {sink.started!r}"
        )
        assert sink.completed == tuple(self._completed), (
            f"completed writes differ from model: {sink.completed!r}"
        )
        assert tuple(failure.stream for failure in worker.failures) == tuple(
            self._failures
        ), "worker failure streams must match the modelled sink failure"
        actual_undelivered = tuple(
            (entry.stream, entry.byte_count) for entry in worker.undelivered_bytes
        )
        expected_undelivered = tuple(self._undelivered.items())
        assert actual_undelivered == expected_undelivered, (
            "shutdown byte totals must match the modelled per-stream groups: "
            f"expected={expected_undelivered!r}, actual={actual_undelivered!r}"
        )

    @precondition(lambda self: self._started and not self._closed)
    @rule(
        stream=st.sampled_from(tuple(EchoStream)),
        source_bytes=st.integers(min_value=1, max_value=16),
    )
    def enqueue(self, stream: EchoStream, source_bytes: int) -> None:
        """Generate accepted, rejected, or post-failure output transitions."""
        worker = self._current_worker()
        item = self._new_item(stream, source_bytes)
        dropped_bytes = worker.enqueue(
            stream,
            self._payload(item),
            flush=True,
            diagnostics=self._diagnostics[stream],
        )
        if self._failed:
            assert dropped_bytes == 0, (
                "a disabled worker accounts later writes as undelivered"
            )
            self._add_undelivered(item)
        elif sum(queued[2] for queued in self._queued) + source_bytes > self._capacity:
            assert dropped_bytes == source_bytes, (
                "a payload beyond queued capacity must be rejected in full"
            )
        else:
            assert dropped_bytes == 0, "a payload that fits must be accepted"
            if self._active is None:
                self._active = item
                self._active_index = self._next_write_index
                self._next_write_index += 1
                self._attempted.append(
                    self._current_sink().wait_for_write(self._active_index),
                )
            else:
                self._queued.append(item)
        self._assert_accounting()

    @precondition(
        lambda self: (
            self._started
            and not self._closed
            and not self._failed
            and self._active is not None
        )
    )
    @rule()
    def complete_active_write(self) -> None:
        """Complete the active write and advance the modelled FIFO once."""
        active = self._active
        assert active is not None, "the rule precondition requires an active item"
        index = self._active_index
        self._current_sink().release(index)
        self._completed.append(active[1])
        if self._queued:
            self._active = self._queued.pop(0)
            self._active_index = index + 1
            self._next_write_index = self._active_index + 1
            started_value = self._current_sink().wait_for_write(self._active_index)
            self._attempted.append(started_value)
        else:
            self._wait_until_idle()
            self._active = None
            self._next_write_index = index + 1
        self._assert_accounting()

    @precondition(
        lambda self: (
            self._started
            and not self._closed
            and not self._failed
            and self._active is not None
        )
    )
    @rule()
    def fail_active_write(self) -> None:
        """Fail an active write and account for every later queued target."""
        active = self._active
        assert active is not None, "the rule precondition requires an active item"
        self._current_sink().release(self._active_index, fail=True)
        worker = self._current_worker()
        deadline = time.monotonic() + 1.0
        with worker._condition:
            while not worker._failures and time.monotonic() < deadline:
                worker._condition.wait(timeout=0.01)
        assert worker.failures, "the selected sink error must disable the worker"
        self._failures.append(active[0])
        for queued_item in self._queued:
            self._add_undelivered(queued_item)
        self._queued.clear()
        self._active = None
        self._failed = True
        self._assert_accounting()

    @precondition(lambda self: self._started and not self._closed)
    @rule(grace=st.sampled_from((0.0, 0.002)))
    def close(self, grace: float) -> None:
        """Close at generated active/idle points and compare loss totals."""
        if self._active is not None:
            self._add_undelivered(self._active)
        for queued_item in self._queued:
            self._add_undelivered(queued_item)
        self._queued.clear()
        self._active = None
        result = self._current_worker().close(grace=grace)
        assert tuple(failure.stream for failure in result.failures) == tuple(
            self._failures
        ), "close must retain the modelled sink failure"
        actual_undelivered = tuple(
            (entry.stream, entry.byte_count) for entry in result.undelivered_bytes
        )
        assert actual_undelivered == tuple(self._undelivered.items()), (
            "close must account for every active and queued source byte"
        )
        self._closed = True
        self._assert_accounting()

    @precondition(lambda self: self._closed)
    @rule(grace=st.sampled_from((0.0, 0.002)))
    def close_again(self, grace: float) -> None:
        """Check close remains idempotent after a generated terminal step."""
        result = self._current_worker().close(grace=grace)
        assert tuple(failure.stream for failure in result.failures) == tuple(
            self._failures
        ), "repeated close must return the original failure snapshot"
        actual_undelivered = tuple(
            (entry.stream, entry.byte_count) for entry in result.undelivered_bytes
        )
        assert actual_undelivered == tuple(self._undelivered.items()), (
            "repeated close must return the original per-stream loss snapshot"
        )
        self._assert_accounting()

    @invariant()
    def worker_matches_model(self) -> None:
        """Keep worker accounting consistent after every generated action."""
        if self._started:
            self._assert_accounting()

    def teardown(self) -> None:
        """Release blocked sink calls and stop the worker after each example."""
        sink = self._sink
        worker = self._worker
        if sink is None or worker is None:
            return
        sink.release_all()
        if not self._closed:
            worker.close(grace=1.0)
        worker._thread.join(timeout=1.0)
        assert not worker._thread.is_alive(), (
            "state-machine cleanup must not leave a delivery thread running"
        )


TestBoundedDeliveryWorker = _DeliveryWorkerMachine.TestCase
TestBoundedDeliveryWorker.settings = settings(
    max_examples=30,
    stateful_step_count=16,
    deadline=None,
)
