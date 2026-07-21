"""Span-lifecycle and attribute-contract tests for ``TracingHook``.

These tests cover the PID-keyed span bookkeeping fixed in issue #122:
recycled PIDs must not orphan the previous span, and the documented
span-attribute contract must stay in lockstep with the attributes the
hook can actually emit. They live in their own module (rather than in
``test_adapters.py``) because they exercise the hook's internal
lifecycle directly instead of the adapter behaviour observed through
command execution.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

from cuprum.adapters.tracing_adapter import InMemorySpan, InMemoryTracer, TracingHook
from cuprum.events import ExecEvent, ExecPhase
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
    line: str | None = None,
    exit_code: int | None = None,
    duration_s: float | None = None,
) -> ExecEvent:
    """Build a minimal ``ExecEvent`` for direct hook dispatch."""
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
    )


def test_recycled_pid_does_not_orphan_previous_span() -> None:
    """A recycled PID after a missed exit closes the stale span."""
    tracer = InMemoryTracer()
    hook = TracingHook(tracer)

    # First execution starts but its exit event is missed (e.g. dropped).
    hook(_exec_event("start", pid=1234))
    # The PID is recycled by a second execution before the first closed.
    hook(_exec_event("start", pid=1234))
    # The second execution exits cleanly.
    hook(_exec_event("exit", pid=1234, exit_code=0, duration_s=0.1))

    assert len(tracer.spans) == 2, "each execution must produce its own span"
    assert all(span.ended for span in tracer.spans), (
        "no span may be left open/orphaned when a PID is recycled"
    )
    stale, current = tracer.spans
    assert stale.status_ok is False, (
        "the span with the missed exit must be ended as unknown/failed"
    )
    assert current.status_ok is True, (
        "the recycled-PID execution must retain its own clean exit status"
    )


def test_recycled_pid_attributes_output_to_correct_span() -> None:
    """Output emitted before and after a PID is recycled attaches correctly.

    Guards the core purpose of the issue #122 fix: when a PID is reused after
    a missed exit, later ``stdout``/``stderr`` events must land on the current
    span, and output recorded before the recycle must remain on the stale
    span rather than leaking onto the wrong execution.
    """
    tracer = InMemoryTracer()
    hook = TracingHook(tracer)

    # First execution starts, emits output, then its exit event is missed.
    hook(_exec_event("start", pid=1234))
    hook(_exec_event("stdout", pid=1234, line="first-run-output"))
    # The PID is recycled by a second execution before the first closed.
    hook(_exec_event("start", pid=1234))
    hook(_exec_event("stdout", pid=1234, line="second-run-output"))
    hook(_exec_event("exit", pid=1234, exit_code=0, duration_s=0.1))

    stale, current = tracer.spans
    assert stale.events == [("cuprum.stdout", {"line": "first-run-output"})], (
        "output from the first run must stay on the stale span"
    )
    assert current.events == [("cuprum.stdout", {"line": "second-run-output"})], (
        "output from the recycled-PID run must attach to the current span"
    )


def test_recycled_pid_transition_ends_stale_before_installing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale span is failed and ended before its replacement is installed.

    Probes the atomic PID→span transition in ``_handle_start``: teardown of
    the orphaned span and installation of its replacement happen under a
    single lock acquisition, so a concurrent reader can never observe a
    half-updated mapping. Ending the stale span runs while the map still
    points at it; the replacement is installed only afterwards.
    """
    tracer = InMemoryTracer()
    hook = TracingHook(tracer)
    hook(_exec_event("start", pid=42))
    stale = tracer.spans[0]

    observed: dict[str, object] = {}
    real_end = InMemorySpan.end

    def recording_end(span: InMemorySpan) -> None:
        """Capture the hook's PID mapping at the moment the stale span ends."""
        if span is stale:
            observed["mapping_during_end"] = hook._active_spans.get(42)
            observed["status_during_end"] = span.status_ok
        real_end(span)

    monkeypatch.setattr(InMemorySpan, "end", recording_end)

    hook(_exec_event("start", pid=42))

    current = tracer.spans[1]
    assert observed["mapping_during_end"] is stale, (
        "the stale span must still be the active mapping while it is ended"
    )
    assert observed["status_during_end"] is False, (
        "the stale span must be marked failed before it is ended"
    )
    assert stale.ended is True, "the stale span must be ended"
    assert hook._active_spans[42] is current, (
        "the replacement span must be installed after the stale span is ended"
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
