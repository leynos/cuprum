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

import re
from pathlib import Path

from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook
from cuprum.events import ExecEvent, ExecPhase
from cuprum.program import Program


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


def test_documented_attributes_match_build_attributes() -> None:
    """The class docstring lists exactly the attributes the code can emit.

    Keeps the documented span-attribute contract in lockstep with
    ``_build_attributes`` plus the exit-time attributes, so the two cannot
    drift (the omission of ``cuprum.cwd`` / ``cuprum.pipeline_stages`` that
    motivated issue #122).
    """
    documented = set(
        re.findall(r"``(cuprum\.[a-z_]+)``", TracingHook.__doc__ or ""),
    )

    # Every attribute the hook can attach: start-time (from
    # _build_attributes over a fully-populated event) plus exit-time.
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
    emitted |= {"cuprum.exit_code", "cuprum.duration_s"}

    assert documented == emitted, (
        "TracingHook docstring attributes must match the emitted set; "
        f"documented-only={documented - emitted}, code-only={emitted - documented}"
    )
