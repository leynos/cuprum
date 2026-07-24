"""Span-lifecycle and attribute-contract tests for ``TracingHook``.

These tests cover the execution-keyed span bookkeeping for issue #122:
lifecycle events are correlated by ``ExecEvent.exec_id``, so a recycled
PID cannot cross one execution's events onto another execution's span,
and the documented span-attribute contract must stay in lockstep with
the attributes the hook can actually emit. They live in their own module
(rather than in ``test_adapters.py``) because they exercise the hook's
internal lifecycle directly instead of the adapter behaviour observed
through command execution.

Events are built with the shared :func:`_make_exec_event` factory; each
call passes its ``pid``, ``exec_id``, and phase-specific fields through
``overrides``. Pass distinct ``exec_id`` tokens for distinct executions
(even when they share a ``pid``), reuse one token across an execution's
phases, and pass ``exec_id=None`` to model a legacy event.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

from cuprum.adapters.tracing_adapter import InMemorySpan, InMemoryTracer, TracingHook
from cuprum.events import ExecEvent, new_exec_id
from cuprum.program import Program
from cuprum.unittests._adapter_test_support import _make_exec_event

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

# A single recycled PID shared by two distinct executions A and B.
_SHARED_PID = 1234


def _users_guide_text() -> str:
    """Return the users' guide markdown for documentation-contract checks."""
    repo_root = Path(__file__).resolve().parents[2]
    return (repo_root / "docs" / "users-guide.md").read_text(encoding="utf-8")


class TestTracingSpanLifecycle:
    """Span-lifecycle and attribute-contract tests for ``TracingHook``."""

    def test_recycled_pid_output_attaches_by_exec_id_not_pid(self) -> None:
        """Delayed output for A never lands on B, despite the shared PID.

        A and B run on the same recycled PID. A's exit is missed, then A emits a
        late ``stdout``; keying by ``exec_id`` routes it to A, never to B.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
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

    def test_recycled_pid_exit_closes_correct_execution(self) -> None:
        """A delayed exit for A closes A and never touches B.

        B, still open on the recycled PID, retains its status until its own exit.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
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

    def test_recycled_pid_normal_flow_for_second_execution(self) -> None:
        """B's own output and exit still attach to and close B's span.

        Even with A left open on the shared PID, the ordinary path for B is
        unaffected.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
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

    def test_legacy_events_without_exec_id_are_ignored(self) -> None:
        """Legacy PID-only events are ignored and cannot disturb a tracked span.

        This locks in the documented policy: without a correlation token an event
        is ambiguous, so the hook drops it rather than attach output/exit to the
        most recent span for the same PID.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
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
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A repeated exec_id ends the prior span after installing the new one.

        Distinct executions carry distinct tokens, so this only guards a malformed
        stream that repeats a token. The replacement is installed under the lock,
        then the detached prior span is failed and ended outside the lock, so the
        exec_id→span mapping already points at the replacement by the time the
        prior span is ended and never exposes a missing entry.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
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

    def test_emitted_attributes_match_documented_contract(self) -> None:
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

    def test_docstring_documents_each_contract_attribute(self) -> None:
        """The ``TracingHook`` docstring names every attribute in the contract.

        Checks substring membership of each documented name rather than parsing
        ``__doc__``, so whitespace or formatting edits to the docstring cannot
        change the test outcome.
        """
        doc = TracingHook.__doc__ or ""
        missing = sorted(
            attr for attr in DOCUMENTED_SPAN_ATTRIBUTES if f"``{attr}``" not in doc
        )
        assert not missing, (
            f"TracingHook docstring omits documented attributes: {missing}"
        )

    def test_users_guide_lists_every_tracing_attribute(self) -> None:
        """The users' guide tracing section names every attribute in the contract.

        Uses inline-code substring membership rather than parsing the list, so the
        guide's markdown formatting is not itself part of the assertion. Locks in
        ``cuprum.cwd`` and ``cuprum.pipeline_stages`` alongside the rest.
        """
        guide = _users_guide_text()
        missing = sorted(
            attr for attr in DOCUMENTED_SPAN_ATTRIBUTES if f"`{attr}`" not in guide
        )
        assert not missing, f"users' guide omits tracing attributes: {missing}"

    def test_users_guide_names_record_output_option(self) -> None:
        """The users' guide documents ``record_output``, not the obsolete name."""
        guide = _users_guide_text()
        assert "record_output" in guide, (
            "users' guide must name the record_output option"
        )
        assert "record_io" not in guide, (
            "users' guide must not use the obsolete record_io option name"
        )
