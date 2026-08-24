"""Concurrency regressions for the ``TracingHook`` span lifecycle.

``TracingHook`` holds its lifecycle lock only across the active-span map, never
across a ``Span`` callback: an arbitrary ``Span`` may block on I/O in
``set_status``/``end``, and holding the lock across that would serialize every
other execution's handlers. Both tests here park a ``Span.end`` and assert what
must still happen while it is parked.

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
