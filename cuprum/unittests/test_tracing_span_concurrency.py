"""Concurrency regression for ``TracingHook`` duplicate-token cleanup.

``TracingHook._handle_start`` swaps the active-span mapping under its
lifecycle lock but marks and ends a *detached* stale span **outside** the
lock. An arbitrary ``Span`` may block on I/O in ``set_status``/``end``, and
holding the lock across that would serialise every other execution's
start/output/exit handler. This test parks a stale span's ``end`` and asserts
an unrelated execution's full lifecycle still completes meanwhile.
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


def _run_full_lifecycle(hook: TracingHook, exec_id: ExecId) -> None:
    """Drive a full start/stdout/exit sequence for one execution."""
    _start(hook, exec_id)
    hook(
        _make_exec_event(
            phase="stdout",
            overrides={"pid": _PID, "exec_id": exec_id, "line": "hi"},
        )
    )
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


def _spawn(target: cabc.Callable[[], None]) -> threading.Thread:
    """Start and return a daemon thread running ``target``."""
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def _block_span_end(
    monkeypatch: pytest.MonkeyPatch,
    target_span: InMemorySpan,
) -> tuple[threading.Event, threading.Event]:
    """Make ``target_span.end()`` block until released.

    Returns ``(entered, release)``: ``entered`` is set when the blocked
    ``end`` is reached, and setting ``release`` lets it proceed. Other spans'
    ``end`` calls are unaffected.
    """
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


def test_duplicate_token_cleanup_does_not_hold_lifecycle_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked stale-span teardown must not stall unrelated executions.

    While the duplicate-token cleanup is parked inside a blocking ``end`` (run
    outside the lifecycle lock), an unrelated execution's start/output/exit
    handlers must still acquire the hook's lock and run to completion.
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
        "an unrelated execution must complete while duplicate-token cleanup is "
        "blocked; the lifecycle lock must not be held during Span.end()"
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
