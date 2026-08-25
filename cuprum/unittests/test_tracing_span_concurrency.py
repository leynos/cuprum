"""Concurrency regressions for the ``TracingHook`` span lifecycle.

``TracingHook`` holds its lifecycle lock only across the active-span map. Each
active execution has a separate lock around its ``Span`` callbacks, so an exit
cannot close one span between lookup and ``add_event`` while callbacks for
unrelated executions remain unblocked.

- ``_handle_start`` swaps the mapping under the lock and marks and ends the
  *detached* stale span outside it, so an unrelated execution's whole lifecycle
  still runs.
- ``_handle_exit`` pops under the lock and ends outside it, so an event that
  loses the race — here a ``pipeline_fail_fast`` arriving once the pop has
  happened — finds no span and is dropped rather than recorded on a span that
  is being closed. Dropping an uncorrelatable event is the hook's documented
  behaviour; the pop is what makes it hold under the race as well as after it.
"""

from __future__ import annotations

import dataclasses as dc
import threading
import typing as typ

from cuprum.adapters.tracing_adapter import InMemorySpan, InMemoryTracer, TracingHook
from cuprum.events import new_exec_id
from cuprum.unittests._adapter_test_support import _make_exec_event

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum.events import ExecId

# A shared recycled PID underscores that only ``exec_id`` distinguishes the
# duplicate-token execution from the unrelated one.
_PID = 4242
_TIMEOUT_S = 5.0


@dc.dataclass(frozen=True, slots=True)
class _BlockedSpanEvent:
    """Controls a paused span event callback and records its state."""

    entered: threading.Event
    release: threading.Event
    was_open: list[bool]


def _start(hook: TracingHook, exec_id: ExecId) -> None:
    """Dispatch a ``start`` event for ``exec_id`` on the shared PID."""
    hook(_make_exec_event(phase="start", overrides={"pid": _PID, "exec_id": exec_id}))


def _fail_fast(hook: TracingHook, exec_id: ExecId) -> None:
    """Dispatch a ``pipeline_fail_fast`` decision for ``exec_id``."""
    hook(
        _make_exec_event(
            phase="pipeline_fail_fast",
            overrides={
                "pid": _PID,
                "exec_id": exec_id,
                "exit_code": 3,
                "duration_s": 0.5,
                "stage_index": 0,
                "stage_count": 2,
            },
        )
    )


def _exit(hook: TracingHook, exec_id: ExecId) -> None:
    """Dispatch a clean ``exit`` for ``exec_id``."""
    hook(
        _make_exec_event(
            phase="exit",
            overrides={
                "pid": _PID,
                "exec_id": exec_id,
                "exit_code": 0,
                "duration_s": 0.1,
            },
        )
    )


def _run_full_lifecycle(hook: TracingHook, exec_id: ExecId) -> None:
    """Drive a full start/stdout/exit sequence for one execution."""
    _start(hook, exec_id)
    hook(
        _make_exec_event(
            phase="stdout",
            overrides={"pid": _PID, "exec_id": exec_id, "line": "hi"},
        )
    )
    _exit(hook, exec_id)


def _spawn(target: cabc.Callable[[], None]) -> threading.Thread:
    """Start and return a daemon thread running ``target``."""
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def _block_span_end(
    monkeypatch: pytest.MonkeyPatch,
    target_span: InMemorySpan,
) -> tuple[threading.Event, threading.Event]:
    """Make ``target_span.end()`` block until released."""
    # Other spans' ``end`` calls are unaffected. Returns a
    # ``(entered, release)`` pair: ``entered`` is set when the blocked
    # ``end`` is reached, and setting ``release`` lets it proceed.
    entered = threading.Event()
    release = threading.Event()
    real_end = InMemorySpan.end

    def blocking_end(span: InMemorySpan) -> None:
        """Park only ``target_span``'s end until the test releases it."""
        if span is target_span:
            entered.set()
            assert release.wait(timeout=_TIMEOUT_S), "blocked end never released"
        real_end(span)

    monkeypatch.setattr(InMemorySpan, "end", blocking_end)
    return entered, release


def _block_span_event(
    monkeypatch: pytest.MonkeyPatch,
    target_span: InMemorySpan,
) -> _BlockedSpanEvent:
    """Make one ``add_event()`` wait and record whether its span was open."""
    entered = threading.Event()
    release = threading.Event()
    was_open: list[bool] = []
    real_add_event = InMemorySpan.add_event

    def blocking_add_event(
        span: InMemorySpan,
        name: str,
        attributes: dict[str, object],
    ) -> None:
        """Pause only the target span's fail-fast event callback."""
        if span is target_span and name == "cuprum.pipeline_fail_fast":
            entered.set()
            assert release.wait(timeout=_TIMEOUT_S), "blocked event never released"
            was_open.append(not span.ended)
        real_add_event(span, name, attributes)

    monkeypatch.setattr(InMemorySpan, "add_event", blocking_add_event)
    return _BlockedSpanEvent(entered, release, was_open)


def _wait_for_span_detachment(hook: TracingHook, exec_id: ExecId) -> bool:
    """Wait briefly for ``exec_id`` to leave the active-span mapping."""
    for _ in range(500):
        if exec_id not in hook._active_spans:
            return True
        threading.Event().wait(0.01)
    return False


class TestTracingSpanConcurrency:
    """Concurrency regression tests for ``TracingHook`` span lifecycle."""

    def test_duplicate_token_cleanup_does_not_hold_lifecycle_lock(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A blocked stale-span teardown must not stall unrelated executions.

        While the duplicate-token cleanup is parked inside a blocking ``end``
        (run outside the lifecycle lock), an unrelated execution's
        start/output/exit handlers must still acquire the hook's lock and run
        to completion.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
        exec_dup = new_exec_id()
        exec_other = new_exec_id()

        # Open exec_dup once so the next start for it triggers duplicate-token
        # cleanup, which ends this (now stale) span.
        _start(hook, exec_dup)
        stale = tracer.spans[0]
        entered, release = _block_span_end(monkeypatch, stale)

        # The reused exec_dup start parks in the (blocked) stale-span end.
        dup_thread = _spawn(lambda: _start(hook, exec_dup))
        assert entered.wait(timeout=_TIMEOUT_S), (
            "the duplicate-token cleanup must reach the blocked stale-span end"
        )

        # The lifecycle lock must be free while cleanup is parked in end(): an
        # unrelated execution completes its whole start/output/exit sequence.
        other_thread = _spawn(lambda: _run_full_lifecycle(hook, exec_other))
        other_thread.join(timeout=_TIMEOUT_S)
        assert not other_thread.is_alive(), (
            "an unrelated execution must complete while duplicate-token cleanup "
            "is blocked; the lifecycle lock must not be held during Span.end()"
        )

        release.set()
        dup_thread.join(timeout=_TIMEOUT_S)
        assert not dup_thread.is_alive(), "duplicate cleanup must finish once released"

        assert stale.ended is True, "the stale span must be ended after release"
        assert stale.status_ok is False, "the stale span must be marked failed"
        assert any(
            span.events == [("cuprum.stdout", {"line": "hi"})]
            and span.ended
            and span.status_ok
            for span in tracer.spans
        ), "the unrelated execution's output and clean exit must be recorded"

    def test_a_fail_fast_racing_its_own_exit_is_dropped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fail-fast event that loses to ``exit`` records nothing.

        ``_handle_exit`` pops the span under the lifecycle lock and only then
        ends it, outside the lock. Parking that ``end`` opens the exact window a
        concurrent ``pipeline_fail_fast`` could fall into: the span object still
        exists and is not yet ended, but it is no longer reachable through the
        active map. The pop is what closes the window — the fail-fast lookup
        misses and the event is dropped, as it is for any event the hook cannot
        correlate, rather than being written onto a span that is being closed.

        An exit that ended the span before detaching it, or that left it in the
        map, would instead record ``cuprum.pipeline_fail_fast`` on a span whose
        backend has already closed it.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
        exec_id = new_exec_id()

        _start(hook, exec_id)
        span = tracer.spans[0]
        entered, release = _block_span_end(monkeypatch, span)

        exit_thread = _spawn(lambda: _exit(hook, exec_id))
        assert entered.wait(timeout=_TIMEOUT_S), (
            "the exit handler must reach the blocked span end"
        )

        # The span has been popped but not yet ended: the racing window.
        _fail_fast(hook, exec_id)

        release.set()
        exit_thread.join(timeout=_TIMEOUT_S)
        assert not exit_thread.is_alive(), "the exit must finish once released"

        recorded = [name for name, _attrs in span.events]
        assert "cuprum.pipeline_fail_fast" not in recorded, (
            "a fail-fast event that lost the race to exit must be dropped, not "
            f"recorded on the closing span; found {span.events!r}"
        )
        assert span.ended is True, "the exit must still end its own span"

    def test_a_fail_fast_is_not_stalled_by_another_span_ending(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A parked ``Span.end`` must not block another execution's fail-fast.

        ``_handle_exit`` ends its span outside the lifecycle lock precisely so
        that an arbitrary backend blocking in ``end()`` cannot serialize every
        other execution's handler. Holding the lock across the pop and the end
        would leave the fail-fast lookup — which takes the same lock — parked
        behind it until the backend returned.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
        exiting = new_exec_id()
        failing = new_exec_id()

        _start(hook, exiting)
        _start(hook, failing)
        exiting_span, failing_span = tracer.spans[0], tracer.spans[1]
        entered, release = _block_span_end(monkeypatch, exiting_span)

        exit_thread = _spawn(lambda: _exit(hook, exiting))
        assert entered.wait(timeout=_TIMEOUT_S), (
            "the exit handler must reach the blocked span end"
        )

        fail_fast_thread = _spawn(lambda: _fail_fast(hook, failing))
        fail_fast_thread.join(timeout=_TIMEOUT_S)
        assert not fail_fast_thread.is_alive(), (
            "an unrelated execution's fail-fast must be recorded while a span "
            "end is parked; the lifecycle lock must not be held during end()"
        )

        release.set()
        exit_thread.join(timeout=_TIMEOUT_S)
        assert not exit_thread.is_alive(), "the exit must finish once released"

        assert failing_span.events == [
            (
                "cuprum.pipeline_fail_fast",
                {
                    "stage_index": 0,
                    "stage_count": 2,
                    "exit_code": 3,
                    "duration_s": 0.5,
                },
            )
        ], (
            "the fail-fast decision must land on its own stage's span, "
            f"found {failing_span.events!r}"
        )

    def test_a_fail_fast_finishes_before_its_own_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A held fail-fast callback keeps only its own span open and ordered."""
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
        failing = new_exec_id()
        unrelated = new_exec_id()
        _start(hook, failing)
        _start(hook, unrelated)
        failing_span, unrelated_span = tracer.spans
        blocked_event = _block_span_event(monkeypatch, failing_span)

        fail_fast_thread = _spawn(lambda: _fail_fast(hook, failing))
        assert blocked_event.entered.wait(timeout=_TIMEOUT_S), (
            "the target fail-fast callback must reach the blocked add_event"
        )

        exit_thread = _spawn(lambda: _exit(hook, failing))
        assert _wait_for_span_detachment(hook, failing), (
            "the exit handler must detach the active span"
        )

        unrelated_thread = _spawn(lambda: _fail_fast(hook, unrelated))
        unrelated_thread.join(timeout=_TIMEOUT_S)
        assert not unrelated_thread.is_alive(), (
            "a target span's paused callback must not block an unrelated span"
        )

        blocked_event.release.set()
        fail_fast_thread.join(timeout=_TIMEOUT_S)
        exit_thread.join(timeout=_TIMEOUT_S)
        assert not fail_fast_thread.is_alive(), "the fail-fast callback must finish"
        assert not exit_thread.is_alive(), "the exit must finish after fail-fast"
        assert blocked_event.was_open == [True], (
            "the fail-fast callback must complete before its span is ended"
        )
        assert failing_span.ended is True, "the target exit must end its span"
        assert unrelated_span.events[0][0] == "cuprum.pipeline_fail_fast", (
            "the unrelated fail-fast callback must remain independently live"
        )
