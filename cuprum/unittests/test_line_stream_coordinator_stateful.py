"""Stateful property checks for the line-stream coordinator's queue handoff.

`_queue_line_sink` posts stamped events onto the run's finite queue,
`_next_queue_item` races that queue against the published result, and
`_coordinate_line_stream` queues the terminal `CommandResult` before publishing
it on the future. This machine drives those production pieces through random
delivery, saturation, draining, completion, cancellation, and failure
sequences against an independent model, so the bounded queue, per-stream
order, single terminal publication, and result/queue agreement are pinned
without processes or a clock. The real-subprocess integration tests keep
covering the lifecycle exits (timeout, early close, and `aclose()`), which need
real children and the wall clock; this machine covers the queue arithmetic
those exits hand back to.

Only the blocking wait is stubbed: each completion rule programs the outcome
the machine's replacement for `_run_to_command_result` returns, raises, or
holds at a gate, so no subprocess is spawned. Everything else — the composed
callback chain, the sink, the queue, the consumer race, and the terminal
publication order — is the production code path.
"""

from __future__ import annotations

import asyncio
import types
import typing as typ

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from cuprum import CommandResult, Program
from cuprum._line_callbacks import _chain_line_hooks
from cuprum._line_iteration import _next_queue_item
from cuprum._line_stream import (
    _coordinate_line_stream,
    _LineStreamTelemetry,
    _observed_line_hook,
    _queue_line_sink,
    coordinator,
)
from cuprum.events import new_exec_id
from cuprum.line_stream_events import LineStreamEvent, LineStreamPhase
from cuprum.line_stream_observation import observe_line_stream
from cuprum.lines import LineEvent, LineStreamName

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._line_stream import _LineStreamRun
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.lines import _LineHookFn

type _StreamName = typ.Literal["stdout", "stderr"]

type _CallbackMode = typ.Literal["none", "sync", "async", "failing"]

_STREAMS: tuple[_StreamName, ...] = ("stdout", "stderr")

_MODES: tuple[_CallbackMode, ...] = ("none", "sync", "async", "failing")


def _command_result(exit_code: int) -> CommandResult:
    """Build the terminal result the stubbed coordinator publishes."""
    return CommandResult(
        program=Program("coordinator"),
        argv=(),
        exit_code=exit_code,
        pid=-1,
        stdout=None,
        stderr=None,
    )


class _LineStreamCoordinatorMachine(RuleBasedStateMachine):
    """Drive random queue handoffs through the coordinator's production pieces."""

    def __init__(self) -> None:
        """Patch the blocking wait and start on a throwaway queue."""
        super().__init__()
        self._loop = asyncio.new_event_loop()
        # `asyncio.ensure_future` in the rules resolves through the running
        # loop rather than the machine's own, so the rules that launch a
        # parked delivery need the machine's loop to be the current one.
        asyncio.set_event_loop(self._loop)
        self._events: list[LineStreamEvent] = []
        self._registration = observe_line_stream(self._events.append)
        self._original_wait = coordinator._run_to_command_result
        coordinator._run_to_command_result = self._programmed_outcome  # ty: ignore[invalid-assignment] - a bound method stands in for the module function

        self._execution_stub = typ.cast("_SubprocessExecution", types.SimpleNamespace())
        self._drive(self._build(1, "none"))

    # -- harness ---------------------------------------------------------

    def _drive[T](self, outcome: cabc.Awaitable[T]) -> T:
        """Run one coroutine or future to completion on the machine's loop."""
        return self._loop.run_until_complete(
            typ.cast("cabc.Coroutine[object, object, T]", outcome)
        )

    def _await_parked(
        self,
        pending: asyncio.Future[None],
    ) -> None:
        """Finish a delivery parked behind a full queue.

        The rules are synchronous, so the parked post — already handed a free
        slot by the caller's drain — is finished through one loop turn rather
        than an ``await`` that no rule can reach.
        """
        self._drive(asyncio.gather(pending))

    def _user_hook(self, mode: _CallbackMode) -> _LineHookFn | None:
        """Build the run's caller callback for the drawn mode."""
        if mode == "none":
            return None
        if mode == "failing":

            def fail_callback(_event: LineEvent) -> None:
                """Raise the run's callback failure on every line."""
                raise self._callback_error

            return typ.cast("_LineHookFn", fail_callback)
        if mode == "sync":

            def sync_callback(event: LineEvent) -> None:
                """Record the line the caller callback observed."""
                self._delivered.append(event)

            return typ.cast("_LineHookFn", sync_callback)

        async def async_callback(event: LineEvent) -> None:
            """Record the line after yielding, as an awaited callback would."""
            await asyncio.sleep(0)
            self._delivered.append(event)

        return typ.cast("_LineHookFn", async_callback)

    async def _build(self, capacity: int, mode: _CallbackMode) -> None:
        """Reset the model onto a fresh bounded queue and result future."""
        self._capacity = capacity
        self._mode = mode
        self._queue: asyncio.Queue[LineEvent | CommandResult] = asyncio.Queue(
            maxsize=capacity
        )
        self._telemetry = _LineStreamTelemetry(
            exec_id=new_exec_id(),
            queue_capacity=capacity,
        )
        user_hook = self._user_hook(mode)
        hooks: list[_LineHookFn | None] = [
            _observed_line_hook(
                user_hook,
                "callback",
                self._telemetry,
            )
            if user_hook is not None
            else None,
            _observed_line_hook(
                _queue_line_sink(self._queue, self._telemetry),
                "queue",
                self._telemetry,
            ),
        ]
        self._composed = typ.cast("_LineHookFn", _chain_line_hooks(hooks))
        self._run_stub = typ.cast(
            "_LineStreamRun",
            types.SimpleNamespace(telemetry=self._telemetry),
        )
        self._future: asyncio.Future[CommandResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._coordinator: asyncio.Task[None] | None = None
        self._gate = asyncio.Event()
        self._gate_hold = False
        self._callback_error = ValueError("programmed callback failure")
        self._delivered: list[LineEvent] = []
        self._programmed_error: BaseException | None = None
        self._programmed_result = _command_result(0)
        self._pending: list[LineEvent] = []
        self._posted: dict[LineStreamName, list[str]] = {"stdout": [], "stderr": []}
        self._consumed: dict[LineStreamName, list[str]] = {"stdout": [], "stderr": []}
        self._terminal_enqueued = False
        self._terminal_consumed = False
        self._run_ended = False
        self._flag = False
        # Baselines over the shared event log: `restart` rebuilds the model but
        # never clears the observer's log, so each counter starts where the
        # previous run left off.
        self._failures_seen = len(self._failures())
        self._saturations = self._count_phase(LineStreamPhase.QUEUE_SATURATED)
        self._cancellations = self._count_phase(LineStreamPhase.CANCELLED)

    def _end_of_stream(self) -> bool:
        """Whether the run has ended and no further line may be delivered."""
        return self._run_ended or self._terminal_enqueued or self._future.done()

    def _count_phase(self, phase: LineStreamPhase) -> int:
        """Count the lifecycle events emitted so far with ``phase``."""
        return sum(event.phase == phase for event in self._events)

    def _failures(self) -> list[LineStreamEvent]:
        """Return the sink-failure events emitted so far."""
        return [
            event
            for event in self._events
            if event.phase == LineStreamPhase.SINK_FAILED
        ]

    async def _programmed_outcome(
        self,
        run: _LineStreamRun,
        execution: _SubprocessExecution,
    ) -> CommandResult:
        """Return the programmed outcome instead of spawning a child."""
        del run, execution
        if self._gate_hold:
            await self._gate.wait()
        if self._programmed_error is not None:
            raise self._programmed_error
        return self._programmed_result

    def _mark_future_retrieved(self) -> None:
        """Retrieve a published failure so the loop does not warn about it."""
        if self._future.done() and not self._future.cancelled():
            self._future.exception()

    def _awaitable(self, outcome: object) -> cabc.Awaitable[None]:
        """Narrow a hook outcome to the awaitable launch helpers return."""
        assert outcome is not None, "the composed chain must return an awaitable"
        return typ.cast("cabc.Awaitable[None]", outcome)

    # -- rules -----------------------------------------------------------

    @initialize(
        capacity=st.integers(min_value=1, max_value=3),
        mode=st.sampled_from(_MODES),
    )
    def _start_run(self, capacity: int, mode: _CallbackMode) -> None:
        """Discard the setup queue and start one with the drawn shape."""
        self._drive(self._build(capacity, mode))

    @precondition(lambda self: self._coordinator is None or self._coordinator.done())
    @rule(
        capacity=st.integers(min_value=1, max_value=3),
        mode=st.sampled_from(_MODES),
    )
    def restart(self, capacity: int, mode: _CallbackMode) -> None:
        """Once nothing is mid-flight, begin a fresh run.

        This keeps the machine making progress after a run drains, and it is
        the release valve for states a later rule cannot consume — a failure
        published over lines that are still queued, for instance.
        """
        if self._future is not None:
            self._mark_future_retrieved()
        self._drive(self._build(capacity, mode))

    def _run_accepts_lines(self) -> bool:
        """Return whether a delivery can land without parking the producer."""
        return (
            self._queue.qsize() < self._capacity
            and self._mode != "failing"
            and not self._end_of_stream()
        )

    @precondition(lambda self: self._run_accepts_lines())
    @rule(
        stream=st.sampled_from(_STREAMS),
        text=st.text(alphabet="abcdef", min_size=1, max_size=6),
    )
    def deliver(self, stream: _StreamName, text: str) -> None:
        """Post one line through the composed chain while there is room."""
        self._drive(self._deliver_coro(stream, text))

    async def _deliver_coro(self, stream: _StreamName, text: str) -> None:
        """Post one line through the production chain and mirror the queue."""
        event = LineEvent(stream=stream, at=0.0, text=text)
        await self._awaitable(self._composed(event))
        self._posted[stream].append(text)
        self._pending.append(event)
        if self._mode != "none":
            assert self._delivered[-1] is event, (
                "the caller callback must observe the line before it is queued"
            )
        self._flag = self._queue.full()
        assert self._queue.qsize() <= self._capacity, (
            "the sink must never grow the queue past its bound"
        )

    def _failing_delivery_can_run(self) -> bool:
        """Return whether a raising caller callback can still deliver."""
        return self._mode == "failing" and not self._end_of_stream()

    @precondition(lambda self: self._failing_delivery_can_run())
    @rule(
        stream=st.sampled_from(_STREAMS),
        text=st.text(alphabet="abcdef", min_size=1, max_size=6),
    )
    def deliver_with_failing_callback(self, stream: _StreamName, text: str) -> None:
        """Deliver under a raising caller callback and assert nothing queues."""
        self._drive(self._failing_delivery_coro(stream, text))

    async def _failing_delivery_coro(self, stream: _StreamName, text: str) -> None:
        """Fail the delivery before the queue ever sees the line."""
        event = LineEvent(stream=stream, at=0.0, text=text)
        size_before = self._queue.qsize()
        with pytest.raises(ValueError, match="programmed callback failure") as caught:
            await self._awaitable(self._composed(event))
        assert caught.value is self._callback_error, (
            "a callback failure must propagate unchanged"
        )
        assert self._queue.qsize() == size_before, (
            "a failed callback must not leave its line queued"
        )
        assert self._delivered == [], (
            "a failing callback must never record a delivered line"
        )
        failures = self._failures()
        assert len(failures) == self._failures_seen + 1, (
            "each callback failure must be reported exactly once"
        )
        reported = failures[-1]
        assert (reported.sink, reported.stream, reported.error_type) == (
            "callback",
            stream,
            "ValueError",
        ), "callback failure must name its sink, stream, and error class"
        self._failures_seen += 1

    def _saturation_can_be_entered(self) -> bool:
        """Return whether the sink can still meet a full queue."""
        return (
            self._queue.qsize() == self._capacity
            and self._mode != "failing"
            and not self._end_of_stream()
        )

    @precondition(lambda self: self._saturation_can_be_entered())
    @rule(
        stream=st.sampled_from(_STREAMS),
        text=st.text(alphabet="abcdef", min_size=1, max_size=6),
    )
    def deliver_while_saturated(self, stream: _StreamName, text: str) -> None:
        """Park one line behind a full queue, then free the slot it needs."""
        # The saturation report fires on the transition into a full queue the
        # posting sink did not itself fill; the modelled flag records whether
        # that report is still owed.
        expected_emissions = 0 if self._flag else 1
        event = LineEvent(stream=stream, at=0.0, text=text)
        pending = asyncio.ensure_future(self._awaitable(self._composed(event)))
        self._drive(asyncio.sleep(0))
        assert not pending.done(), "a full queue must park the consumer sink"

        # The drain runs while the parked post is still blocked, mirroring an
        # iterator freeing one slot as the child keeps writing.
        head = self._pending.pop(0)
        self._queue.get_nowait()
        self._consumed[head.stream].append(head.text)
        self._await_parked(pending)

        emissions = self._count_phase(LineStreamPhase.QUEUE_SATURATED)
        assert emissions == self._saturations + expected_emissions, (
            "saturation must be reported once per unreported episode"
        )
        if expected_emissions:
            reported = [
                event
                for event in self._events
                if event.phase == LineStreamPhase.QUEUE_SATURATED
            ][-1]
            assert reported.stream == stream, (
                "saturation must name the stream whose post parked"
            )
            assert reported.queue_size == self._capacity, (
                "saturation must report the queue's finite bound"
            )
        self._saturations = emissions
        self._posted[stream].append(text)
        self._pending.append(event)
        self._flag = self._queue.full()
        assert self._queue.qsize() == self._capacity, (
            "the released post must refill the one slot the drain freed"
        )

    @precondition(lambda self: self._run_accepts_lines())
    @rule(
        stream=st.sampled_from(_STREAMS),
        text=st.text(alphabet="abcdef", min_size=1, max_size=6),
    )
    def fill_directly(self, stream: _StreamName, text: str) -> None:
        """Fill the remaining slots without the sink, leaving the flag unset.

        The saturation report fires when a post first encounters a full queue
        it did not itself fill; the focused
        `test_queue_saturation_reports_bounded_queue_details` reaches that
        state by filling directly, and this rule reproduces the same input
        shape so the machine can exercise the positive case.
        """
        while self._queue.qsize() < self._capacity:
            event = LineEvent(stream=stream, at=0.0, text=text)
            self._queue.put_nowait(event)
            self._posted[stream].append(text)
            self._pending.append(event)

    def _completion_can_run(self) -> bool:
        """Return whether a successful result can be published."""
        return (
            (self._coordinator is None or self._coordinator.done())
            and not self._future.done()
            # The coordinator's terminal post is awaited, so a full queue would
            # park it until the iterator drains a slot; keep the queue short of
            # its bound here and let the consume rules model that drain.
            and self._queue.qsize() < self._capacity
        )

    @precondition(lambda self: self._completion_can_run())
    @rule(exit_code=st.integers(min_value=0, max_value=3))
    def complete(self, exit_code: int) -> None:
        """Publish a successful result behind any lines still queued."""
        self._programmed_error = None
        self._programmed_result = _command_result(exit_code)
        self._drive(self._coordinate_coro())
        assert self._future.result() is self._programmed_result, (
            "publication must agree with the terminal queue item"
        )
        self._terminal_enqueued = True
        assert self._queue.qsize() == len(self._pending) + 1, (
            "the terminal result must be queued before it is published"
        )

    def _failure_can_run(self) -> bool:
        """Return whether a failing result can be published."""
        return (
            self._coordinator is None or self._coordinator.done()
        ) and not self._future.done()

    @precondition(lambda self: self._failure_can_run())
    @rule()
    def fail(self) -> None:
        """Publish a failure that never lands a terminal item on the queue."""
        self._programmed_error = ValueError("programmed coordinator failure")
        self._drive(self._coordinate_coro())
        self._run_ended = True
        assert self._future.exception() is self._programmed_error, (
            "the published failure must be the original error"
        )
        assert not self._terminal_enqueued, (
            "a failed run must not queue a terminal result"
        )

    async def _coordinate_coro(self) -> None:
        """Run the production coordinator to its programmed outcome."""
        self._coordinator = asyncio.create_task(
            _coordinate_line_stream(
                self._run_stub,
                self._execution_stub,
                self._queue,
                self._future,
            )
        )
        await asyncio.gather(self._coordinator)

    @precondition(lambda self: self._coordinator is None and not self._future.done())
    @rule()
    def cancel_mid_flight(self) -> None:
        """Cancel the coordinator before it publishes, as teardown does."""
        self._drive(self._cancel_coro())
        self._run_ended = True
        assert self._future.done() is False, (
            "a cancelled coordinator must not publish an outcome"
        )
        reported = self._count_phase(LineStreamPhase.CANCELLED)
        assert reported == self._cancellations + 1, (
            "cancelling the coordinator must report one cancellation"
        )
        self._cancellations = reported

    async def _cancel_coro(self) -> None:
        """Park the coordinator at its gate, cancel it, and absorb the end."""
        self._gate_hold = True
        self._coordinator = asyncio.create_task(
            _coordinate_line_stream(
                self._run_stub,
                self._execution_stub,
                self._queue,
                self._future,
            )
        )
        await asyncio.sleep(0)
        assert not self._coordinator.done(), (
            "the gated coordinator must still be running before the cancel"
        )
        self._coordinator.cancel()
        await asyncio.gather(self._coordinator, return_exceptions=True)
        assert self._coordinator.cancelled(), (
            "teardown cancellation must end the coordinator cancelled"
        )
        self._gate_hold = False

    @precondition(
        lambda self: (
            self._queue.qsize() > 0
            and not (
                self._future.done()
                and not self._future.cancelled()
                and self._future.exception() is not None
            )
        )
    )
    @rule()
    def consume(self) -> None:
        """Consume one item through the production consumer half."""
        item = self._drive(_next_queue_item(self._queue, self._future))
        if isinstance(item, CommandResult):
            assert not self._terminal_consumed, (
                "iteration must observe exactly one terminal outcome"
            )
            assert self._future.result() is item, (
                "the terminal queue item must be the published result"
            )
            assert item is self._programmed_result, (
                "the terminal queue item must be the programmed result"
            )
            self._terminal_consumed = True
            return
        head = self._pending.pop(0)
        assert item is head, "the queue must deliver lines in post order"
        self._consumed[item.stream].append(item.text)

    @precondition(
        lambda self: (
            self._queue.qsize() == 0
            and self._future.done()
            and not self._future.cancelled()
            and self._future.exception() is not None
        )
    )
    @rule()
    def consume_published_failure(self) -> None:
        """Raise the published failure out of a drained queue consumption."""
        failure = self._future.exception()
        assert failure is not None, "the precondition must hold a failure"
        with pytest.raises(type(failure)) as caught:
            self._drive(_next_queue_item(self._queue, self._future))
        assert caught.value is failure, (
            "the consumer must re-raise the published failure unchanged"
        )

    # -- invariants ------------------------------------------------------

    @invariant()
    def queue_stays_bounded(self) -> None:
        """Keep the finite queue within the capacity it was built with."""
        assert 0 <= self._queue.qsize() <= self._capacity, (
            "the queue must stay within its finite bound"
        )

    @invariant()
    def queue_contents_match_the_model(self) -> None:
        """Queued items are the modelled lines plus the terminal result once."""
        expected = len(self._pending) + (
            1 if self._terminal_enqueued and not self._terminal_consumed else 0
        )
        assert self._queue.qsize() == expected, (
            "the queue must hold the modelled lines and at most one terminal"
        )

    @invariant()
    def streams_preserve_delivery_order(self) -> None:
        """Each stream's consumed lines are a prefix of what was posted."""
        for stream in _STREAMS:
            consumed = self._consumed[stream]
            posted = self._posted[stream]
            assert consumed == posted[: len(consumed)], (
                f"{stream} lines must be consumed in the order posted"
            )

    @invariant()
    def saturation_flag_tracks_the_sink(self) -> None:
        """Mirror the telemetry's saturation flag in the modelled flag."""
        assert self._telemetry.is_queue_saturated == self._flag, (
            "the modelled flag must mirror the telemetry's saturation flag"
        )

    @invariant()
    def coordinator_is_never_left_running(self) -> None:
        """Settle every coordinator before the machine steps on."""
        assert self._coordinator is None or self._coordinator.done(), (
            "no rule may leave the coordinator running between steps"
        )

    def teardown(self) -> None:
        """Restore the patched wait, detach the observer, and close the loop."""
        if self._coordinator is not None and not self._coordinator.done():
            self._coordinator.cancel()
            self._drive(asyncio.gather(self._coordinator, return_exceptions=True))
        self._mark_future_retrieved()
        self._registration.detach()
        coordinator._run_to_command_result = self._original_wait  # ty: ignore[invalid-assignment] - restore the saved module function
        self._loop.close()
        # Leave the thread as the rules found it: no loop installed, so a later
        # test's `asyncio.run` never inherits this machine's closed loop.
        asyncio.set_event_loop(None)


TestLineStreamCoordinator = _LineStreamCoordinatorMachine.TestCase
TestLineStreamCoordinator.settings = settings(
    max_examples=40,
    stateful_step_count=16,
    deadline=None,
)
