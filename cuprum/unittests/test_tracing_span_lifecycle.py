"""Span-lifecycle and attribute-contract tests for ``TracingHook``.

These tests cover the execution-keyed span bookkeeping for issue #122:
lifecycle events are correlated by ``ExecEvent.exec_id``, so a recycled
PID cannot cross one execution's events onto another execution's span,
and the documented span-attribute contract must stay in lockstep with
the attributes the hook can actually emit. They live in their own module
(rather than in ``test_adapters.py``) because they exercise the hook's
internal lifecycle directly instead of the adapter behaviour observed
through command execution.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

from cuprum.adapters.tracing_adapter import InMemorySpan, InMemoryTracer, TracingHook
from cuprum.events import ExecEvent, ExecId, ExecPhase, new_exec_id
from cuprum.program import Program

if typ.TYPE_CHECKING:
    import pytest

# Single source of truth for the span-attribute contract documented on
# ``TracingHook``. Both the documentation check and the emitted-attribute
# check derive from this constant, so the contract is defined in exactly one
# place and cannot drift between prose and code.
DOCUMENTED_SPAN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "cuprum.program",
        "cuprum.argv",
        "cuprum.pid",
        "cuprum.cwd",
        "cuprum.exit_code",
        "cuprum.duration_s",
        "cuprum.project",
        "cuprum.pipeline_stage_index",
        "cuprum.pipeline_stages",
    },
)


def _exec_event(
    phase: ExecPhase,
    pid: int,
    *,
    exec_id: ExecId | None = None,
    line: str | None = None,
    exit_code: int | None = None,
    duration_s: float | None = None,
) -> ExecEvent:
    """Build a minimal ``ExecEvent`` for direct hook dispatch.

    ``exec_id`` is the per-execution correlation token: pass distinct tokens
    for distinct executions (even when they share a ``pid``) and reuse one
    token across an execution's phases. ``None`` models a legacy event that
    predates the token.
    """
    return ExecEvent(
        phase=phase,
        program=Program("cat"),
        argv=("cat",),
        cwd=None,
        env=None,
        pid=pid,
        timestamp=0.0,
        line=line,
        exit_code=exit_code,
        duration_s=duration_s,
        tags={},
        exec_id=exec_id,
    )


# A single recycled PID shared by two distinct executions A and B.
_SHARED_PID = 1234


def test_recycled_pid_output_attaches_by_exec_id_not_pid() -> None:
    """Delayed output for A never lands on B, despite the shared PID.

    A and B run on the same recycled PID. A's exit is missed, then A emits a
    late ``stdout``; keying by ``exec_id`` routes it to A, never to B.
    """
    tracer = InMemoryTracer()
    hook = TracingHook(tracer)
    exec_a = new_exec_id()
    exec_b = new_exec_id()

    hook(_exec_event("start", pid=_SHARED_PID, exec_id=exec_a))
    span_a = tracer.spans[0]
    hook(_exec_event("start", pid=_SHARED_PID, exec_id=exec_b))
    span_b = tracer.spans[1]

    # Out-of-order output: A's delayed line arrives after B started.
    hook(_exec_event("stdout", pid=_SHARED_PID, exec_id=exec_a, line="from-A"))
    hook(_exec_event("stdout", pid=_SHARED_PID, exec_id=exec_b, line="from-B"))

    assert span_a.events == [("cuprum.stdout", {"line": "from-A"})], (
        "A's delayed output must attach to A's span"
    )
    assert span_b.events == [("cuprum.stdout", {"line": "from-B"})], (
        "B's span must only receive B's output, never A's delayed line"
    )


def test_recycled_pid_exit_closes_correct_execution() -> None:
    """A delayed exit for A closes A and never touches B.

    B, still open on the recycled PID, retains its status until its own exit.
    """
    tracer = InMemoryTracer()
    hook = TracingHook(tracer)
    exec_a = new_exec_id()
    exec_b = new_exec_id()

    hook(_exec_event("start", pid=_SHARED_PID, exec_id=exec_a))
    span_a = tracer.spans[0]
    hook(_exec_event("start", pid=_SHARED_PID, exec_id=exec_b))
    span_b = tracer.spans[1]

    # A's delayed, failing exit arrives after B recycled the PID.
    hook(
        _exec_event(
            "exit", pid=_SHARED_PID, exec_id=exec_a, exit_code=3, duration_s=0.2
        ),
    )

    assert span_a.ended is True, "A's delayed exit must close A"
    assert span_a.status_ok is False, "A must close with its own failing status"
    assert span_b.ended is False, "A's exit must not close B"
    assert span_b.status_ok is None, "A's exit must not change B's status"

    # B exits cleanly and closes its own span.
    hook(
        _exec_event(
            "exit", pid=_SHARED_PID, exec_id=exec_b, exit_code=0, duration_s=0.1
        ),
    )
    assert span_b.ended is True, "B's exit must close B"
    assert span_b.status_ok is True, "B must retain its own clean status"


def test_recycled_pid_normal_flow_for_second_execution() -> None:
    """B's own output and exit still attach to and close B's span.

    Even with A left open on the shared PID, the ordinary path for B is
    unaffected.
    """
    tracer = InMemoryTracer()
    hook = TracingHook(tracer)
    exec_a = new_exec_id()
    exec_b = new_exec_id()

    hook(_exec_event("start", pid=_SHARED_PID, exec_id=exec_a))  # A left open
    hook(_exec_event("start", pid=_SHARED_PID, exec_id=exec_b))
    span_b = tracer.spans[1]

    hook(_exec_event("stdout", pid=_SHARED_PID, exec_id=exec_b, line="hello-from-B"))
    hook(
        _exec_event(
            "exit", pid=_SHARED_PID, exec_id=exec_b, exit_code=0, duration_s=0.1
        ),
    )

    assert span_b.events == [("cuprum.stdout", {"line": "hello-from-B"})], (
        "B's output must attach to B's span"
    )
    assert span_b.ended is True, "B's exit must close B's span"
    assert span_b.status_ok is True, "B's clean exit must mark its span ok"


def test_legacy_events_without_exec_id_are_ignored() -> None:
    """Legacy PID-only events are ignored and cannot disturb a tracked span.

    This locks in the documented policy: without a correlation token an event
    is ambiguous, so the hook drops it rather than attach output/exit to the
    most recent span for the same PID.
    """
    tracer = InMemoryTracer()
    hook = TracingHook(tracer)
    exec_b = new_exec_id()

    # A live, correlated execution B on the PID.
    hook(_exec_event("start", pid=_SHARED_PID, exec_id=exec_b))
    span_b = tracer.spans[0]

    # Legacy events on the same PID (no exec_id) must all be dropped.
    hook(_exec_event("start", pid=_SHARED_PID, exec_id=None))
    hook(_exec_event("stdout", pid=_SHARED_PID, exec_id=None, line="legacy"))
    hook(
        _exec_event("exit", pid=_SHARED_PID, exec_id=None, exit_code=0, duration_s=0.1),
    )

    assert len(tracer.spans) == 1, "a legacy start must not create a span"
    assert span_b.events == [], "legacy output must not attach to B"
    assert span_b.ended is False, "legacy exit must not close B"
    assert span_b.status_ok is None, "legacy exit must not change B's status"


def test_duplicate_exec_id_start_ends_prior_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated exec_id ends the prior span before installing the new one.

    Distinct executions carry distinct tokens, so this only guards a malformed
    stream that repeats a token. The prior span is failed and ended before the
    replacement is installed, all under one lock acquisition, so the
    exec_id→span mapping never exposes a half-updated entry: the mapping still
    points at the prior span while it is ended, and at the replacement only
    afterwards.
    """
    tracer = InMemoryTracer()
    hook = TracingHook(tracer)
    exec_id = new_exec_id()
    hook(_exec_event("start", pid=42, exec_id=exec_id))
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

    hook(_exec_event("start", pid=42, exec_id=exec_id))

    current = tracer.spans[1]
    assert observed["mapping_during_end"] is stale, (
        "the prior span must still be mapped while it is ended"
    )
    assert observed["status_during_end"] is False, (
        "the prior span must be marked failed before it is ended"
    )
    assert stale.ended is True, "the prior span must be ended"
    assert hook._active_spans[exec_id] is current, (
        "the replacement span must be installed after the prior span is ended"
    )


def test_emitted_attributes_match_documented_contract() -> None:
    """The attributes the hook emits equal the documented contract.

    Every attribute ``_build_attributes`` produces for a fully-populated
    start event, plus the exit-time attributes, must equal
    ``DOCUMENTED_SPAN_ATTRIBUTES`` — guarding the omission of ``cuprum.cwd`` /
    ``cuprum.pipeline_stages`` that motivated issue #122.
    """
    start_event = ExecEvent(
        phase="start",
        program=Program("cat"),
        argv=("cat",),
        cwd=Path("/srv/work"),
        env=None,
        pid=4321,
        timestamp=0.0,
        line=None,
        exit_code=None,
        duration_s=None,
        tags={
            "project": "doc-lockstep",
            "pipeline_stage_index": 0,
            "pipeline_stages": 2,
        },
    )
    emitted = set(TracingHook._build_attributes(start_event))
    # exit_code and duration_s are attached later, in _handle_exit.
    emitted |= {"cuprum.exit_code", "cuprum.duration_s"}

    contract = set(DOCUMENTED_SPAN_ATTRIBUTES)
    assert emitted == contract, (
        "emitted attributes must match the documented contract; "
        f"emitted-only={emitted - contract}, contract-only={contract - emitted}"
    )


def test_docstring_documents_each_contract_attribute() -> None:
    """The ``TracingHook`` docstring names every attribute in the contract.

    Checks substring membership of each documented name rather than parsing
    ``__doc__``, so whitespace or formatting edits to the docstring cannot
    change the test outcome.
    """
    doc = TracingHook.__doc__ or ""
    missing = sorted(
        attr for attr in DOCUMENTED_SPAN_ATTRIBUTES if f"``{attr}``" not in doc
    )
    assert not missing, f"TracingHook docstring omits documented attributes: {missing}"
