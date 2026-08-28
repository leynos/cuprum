"""Correlation of span lifecycle events by ``ExecEvent.exec_id``.

The operating system recycles process identifiers, so a ``pid`` cannot tell
two executions apart: a delayed event from a finished execution would attach
to — or close — the span of a later one that happened to reuse its number.
Spans are therefore keyed by ``exec_id``, a token minted once per execution
(issue #122), and these tests pin that: events for one execution never reach
another's span even when both report the same ``pid``, a repeated token
retires the span it displaces, and an event carrying no token at all is
dropped rather than guessed at.

Split from ``test_tracing_span_lifecycle``, which covers what happens to a
span between ``start`` and ``exit``; this module is about which span an event
belongs to in the first place.

Events are built with the shared :func:`_make_exec_event` factory; each call
passes its ``pid``, ``exec_id``, and phase-specific fields through
``overrides``. Pass distinct ``exec_id`` tokens for distinct executions (even
when they share a ``pid``), reuse one token across an execution's phases, and
pass ``exec_id=None`` to model a legacy event.
"""

from __future__ import annotations

import typing as typ

from cuprum.adapters.tracing_adapter import InMemorySpan
from cuprum.events import new_exec_id
from cuprum.unittests._adapter_test_support import (
    Traced,
    _make_exec_event,
    tracing_hook,
)

if typ.TYPE_CHECKING:
    import pytest

__all__ = ["tracing_hook"]

# A single recycled PID shared by two distinct executions A and B.
_SHARED_PID = 1234


class TestTracingExecIdCorrelation:
    """Events must reach the span of the execution that produced them."""

    def test_recycled_pid_output_attaches_by_exec_id_not_pid(
        self,
        tracing_hook: Traced,
    ) -> None:
        """Delayed output for A never lands on B, despite the shared PID.

        A and B run on the same recycled PID. A's exit is missed, then A emits a
        late ``stdout``; keying by ``exec_id`` routes it to A, never to B.
        """
        tracer, hook = tracing_hook
        exec_a = new_exec_id()
        exec_b = new_exec_id()

        hook(
            _make_exec_event(
                phase="start", overrides={"pid": _SHARED_PID, "exec_id": exec_a}
            )
        )
        span_a = tracer.spans[0]
        hook(
            _make_exec_event(
                phase="start", overrides={"pid": _SHARED_PID, "exec_id": exec_b}
            )
        )
        span_b = tracer.spans[1]

        # Out-of-order output: A's delayed line arrives after B started.
        hook(
            _make_exec_event(
                phase="stdout",
                overrides={"pid": _SHARED_PID, "exec_id": exec_a, "line": "from-A"},
            )
        )
        hook(
            _make_exec_event(
                phase="stdout",
                overrides={"pid": _SHARED_PID, "exec_id": exec_b, "line": "from-B"},
            )
        )

        assert span_a.events == [("cuprum.stdout", {"line": "from-A"})], (
            "A's delayed output must attach to A's span"
        )
        assert span_b.events == [("cuprum.stdout", {"line": "from-B"})], (
            "B's span must only receive B's output, never A's delayed line"
        )

    def test_recycled_pid_exit_closes_correct_execution(
        self,
        tracing_hook: Traced,
    ) -> None:
        """A delayed exit for A closes A and never touches B.

        B, still open on the recycled PID, retains its status until its own exit.
        """
        tracer, hook = tracing_hook
        exec_a = new_exec_id()
        exec_b = new_exec_id()

        hook(
            _make_exec_event(
                phase="start", overrides={"pid": _SHARED_PID, "exec_id": exec_a}
            )
        )
        span_a = tracer.spans[0]
        hook(
            _make_exec_event(
                phase="start", overrides={"pid": _SHARED_PID, "exec_id": exec_b}
            )
        )
        span_b = tracer.spans[1]

        # A's delayed, failing exit arrives after B recycled the PID.
        hook(
            _make_exec_event(
                phase="exit",
                overrides={
                    "pid": _SHARED_PID,
                    "exec_id": exec_a,
                    "exit_code": 3,
                    "duration_s": 0.2,
                },
            )
        )

        assert span_a.ended is True, "A's delayed exit must close A"
        assert span_a.status_ok is False, "A must close with its own failing status"
        assert span_b.ended is False, "A's exit must not close B"
        assert span_b.status_ok is None, "A's exit must not change B's status"

        # B exits cleanly and closes its own span.
        hook(
            _make_exec_event(
                phase="exit",
                overrides={
                    "pid": _SHARED_PID,
                    "exec_id": exec_b,
                    "exit_code": 0,
                    "duration_s": 0.1,
                },
            )
        )
        assert span_b.ended is True, "B's exit must close B"
        assert span_b.status_ok is True, "B must retain its own clean status"

    def test_recycled_pid_normal_flow_for_second_execution(
        self,
        tracing_hook: Traced,
    ) -> None:
        """B's own output and exit still attach to and close B's span.

        Even with A left open on the shared PID, the ordinary path for B is
        unaffected.
        """
        tracer, hook = tracing_hook
        exec_a = new_exec_id()
        exec_b = new_exec_id()

        # A left open.
        hook(
            _make_exec_event(
                phase="start", overrides={"pid": _SHARED_PID, "exec_id": exec_a}
            )
        )
        hook(
            _make_exec_event(
                phase="start", overrides={"pid": _SHARED_PID, "exec_id": exec_b}
            )
        )
        span_b = tracer.spans[1]

        hook(
            _make_exec_event(
                phase="stdout",
                overrides={
                    "pid": _SHARED_PID,
                    "exec_id": exec_b,
                    "line": "hello-from-B",
                },
            )
        )
        hook(
            _make_exec_event(
                phase="exit",
                overrides={
                    "pid": _SHARED_PID,
                    "exec_id": exec_b,
                    "exit_code": 0,
                    "duration_s": 0.1,
                },
            )
        )

        assert span_b.events == [("cuprum.stdout", {"line": "hello-from-B"})], (
            "B's output must attach to B's span"
        )
        assert span_b.ended is True, "B's exit must close B's span"
        assert span_b.status_ok is True, "B's clean exit must mark its span ok"

    def test_legacy_events_without_exec_id_are_ignored(
        self,
        tracing_hook: Traced,
    ) -> None:
        """Legacy PID-only events are ignored and cannot disturb a tracked span.

        This locks in the documented policy: without a correlation token an event
        is ambiguous, so the hook drops it rather than attach output/exit to the
        most recent span for the same PID.
        """
        tracer, hook = tracing_hook
        exec_b = new_exec_id()

        # A live, correlated execution B on the PID.
        hook(
            _make_exec_event(
                phase="start", overrides={"pid": _SHARED_PID, "exec_id": exec_b}
            )
        )
        span_b = tracer.spans[0]

        # Legacy events on the same PID (no exec_id) must all be dropped.
        hook(
            _make_exec_event(
                phase="start", overrides={"pid": _SHARED_PID, "exec_id": None}
            )
        )
        hook(
            _make_exec_event(
                phase="stdout",
                overrides={"pid": _SHARED_PID, "exec_id": None, "line": "legacy"},
            )
        )
        hook(
            _make_exec_event(
                phase="exit",
                overrides={
                    "pid": _SHARED_PID,
                    "exec_id": None,
                    "exit_code": 0,
                    "duration_s": 0.1,
                },
            )
        )

        assert len(tracer.spans) == 1, "a legacy start must not create a span"
        assert span_b.events == [], "legacy output must not attach to B"
        assert span_b.ended is False, "legacy exit must not close B"
        assert span_b.status_ok is None, "legacy exit must not change B's status"

    def test_duplicate_exec_id_start_ends_prior_span(
        self,
        tracing_hook: Traced,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A repeated exec_id ends the prior span after installing the new one.

        Distinct executions carry distinct tokens, so this only guards a malformed
        stream that repeats a token. The replacement is installed under the lock,
        then the detached prior span is failed and ended outside the lock, so the
        exec_id→span mapping already points at the replacement by the time the
        prior span is ended and never exposes a missing entry.
        """
        tracer, hook = tracing_hook
        exec_id = new_exec_id()
        hook(_make_exec_event(phase="start", overrides={"pid": 42, "exec_id": exec_id}))
        stale = tracer.spans[0]

        observed: dict[str, object] = {}
        real_end = InMemorySpan.end

        def recording_end(span: InMemorySpan) -> None:
            """Capture the hook's mapping at the moment the prior span ends."""
            if span is stale:
                observed["mapping_during_end"] = hook._active_spans.get(exec_id)
                observed["status_during_end"] = span.status_ok
            real_end(span)

        monkeypatch.setattr(InMemorySpan, "end", recording_end)

        hook(_make_exec_event(phase="start", overrides={"pid": 42, "exec_id": exec_id}))

        current = tracer.spans[1]
        assert observed["mapping_during_end"] is current, (
            "the replacement must be installed before the detached prior span "
            "is ended outside the lock"
        )
        assert observed["status_during_end"] is False, (
            "the prior span must be marked failed before it is ended"
        )
        assert stale.ended is True, "the prior span must be ended"
        assert hook._active_spans[exec_id] is current, (
            "the replacement span must remain installed after the prior span ends"
        )

    def test_events_without_exec_id_are_ignored(self, tracing_hook: Traced) -> None:
        """Legacy events lacking an exec_id are ignored (uncorrelatable).

        Without a correlation token the hook cannot know which execution an
        event belongs to, so it declines to trace it rather than risk
        attaching output/exit to an unrelated PID's span.
        """
        tracer, hook = tracing_hook

        base = {"program": "echo", "argv": ("echo", "hello"), "pid": 4321}
        # A full lifecycle, but every event predates the correlation token.
        hook(_make_exec_event(phase="start", overrides={**base, "exec_id": None}))
        hook(
            _make_exec_event(
                phase="stdout",
                overrides={**base, "exec_id": None, "line": "hello"},
            ),
        )
        hook(
            _make_exec_event(
                phase="exit",
                overrides={**base, "exec_id": None, "exit_code": 0, "duration_s": 0.1},
            ),
        )

        assert tracer.spans == [], (
            "events without an exec_id must not create or affect any span"
        )
