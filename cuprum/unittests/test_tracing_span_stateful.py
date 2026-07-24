"""Hypothesis stateful coverage for ``TracingHook`` span correlation.

The deterministic tests in ``test_tracing_span_lifecycle`` pin specific
recycled-PID orderings. This state machine generalises their invariant:
across randomised, interleaved sequences of ``start``/``stdout``/``stderr``/
``exit`` events for two ``exec_id`` values that deliberately share one PID
(plus uncorrelated ``exec_id=None`` events), correlation by ``exec_id`` must
hold. Output only ever lands on its own execution's span, an exit ends and
removes only its own execution's span, and legacy events create, modify, and
close nothing.

The machine keeps a small model keyed by ``exec_id`` (the currently-active
span for each) and cross-checks it against the hook's internal state after
every step.
"""

from __future__ import annotations

import typing as typ

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook
from cuprum.events import new_exec_id
from cuprum.unittests._adapter_test_support import _make_exec_event

if typ.TYPE_CHECKING:
    from cuprum.adapters.tracing_adapter import InMemorySpan
    from cuprum.events import ExecId, ExecPhase

# Two executions A and B are driven on one recycled PID so that only the
# ``exec_id`` distinguishes them.
_SHARED_PID = 4242
_TOKENS = ("A", "B")
_LINES = st.text(alphabet="abcdef", max_size=6)
_STREAMS = ("stdout", "stderr")


class TracingSpanMachine(RuleBasedStateMachine):
    """Drive interleaved lifecycle events for two exec_ids sharing one PID."""

    def __init__(self) -> None:
        """Start with a fresh hook and an empty per-exec_id model."""
        super().__init__()
        self.tracer = InMemoryTracer()
        self.hook = TracingHook(self.tracer)
        self._ids: dict[str, ExecId] = {name: new_exec_id() for name in _TOKENS}
        # Currently-active span per exec_id, maintained independently of the
        # hook and cross-checked against ``hook._active_spans`` each step.
        self.active: dict[ExecId, InMemorySpan] = {}

    def _events_snapshot(self) -> dict[int, list[tuple[str, dict[str, object]]]]:
        """Copy every span's recorded events, keyed by object identity."""
        return {id(span): list(span.events) for span in self.tracer.spans}

    def _status_snapshot(self) -> dict[int, tuple[bool, bool | None]]:
        """Copy every span's ``(ended, status_ok)``, keyed by object identity."""
        return {id(span): (span.ended, span.status_ok) for span in self.tracer.spans}

    @rule(name=st.sampled_from(_TOKENS))
    def start(self, name: str) -> None:
        """Open one span for a ``start`` without touching other executions."""
        exec_id = self._ids[name]
        prior = self.active.get(exec_id)
        before_status = self._status_snapshot()
        count = len(self.tracer.spans)

        self.hook(
            _make_exec_event(
                phase="start", overrides={"pid": _SHARED_PID, "exec_id": exec_id}
            )
        )

        assert len(self.tracer.spans) == count + 1, "start must open exactly one span"
        new_span = self.tracer.spans[-1]
        for span in self.tracer.spans[:-1]:
            if prior is not None and span is prior:
                assert (span.ended, span.status_ok) == (True, False), (
                    "a duplicate exec_id must fail and end the prior span"
                )
            else:
                assert (span.ended, span.status_ok) == before_status[id(span)], (
                    "start must not touch another execution's span"
                )
        self.active[exec_id] = new_span

    @rule(
        name=st.sampled_from(_TOKENS),
        stream=st.sampled_from(_STREAMS),
        line=_LINES,
    )
    def output(self, name: str, stream: str, line: str) -> None:
        """Output may only append to the matching execution's active span."""
        exec_id = self._ids[name]
        target = self.active.get(exec_id)
        before = self._events_snapshot()

        self.hook(
            _make_exec_event(
                phase=typ.cast("ExecPhase", stream),
                overrides={"pid": _SHARED_PID, "exec_id": exec_id, "line": line},
            )
        )

        recorded = (f"cuprum.{stream}", {"line": line})
        for span in self.tracer.spans:
            if target is not None and span is target:
                assert span.events == [*before[id(span)], recorded], (
                    "output must append to the matching execution's span"
                )
            else:
                assert span.events == before[id(span)], (
                    "output must not appear on any other span"
                )

    @rule(
        name=st.sampled_from(_TOKENS),
        exit_code=st.integers(min_value=0, max_value=5),
    )
    def finish(self, name: str, exit_code: int) -> None:
        """End and remove only the matching execution's span on ``exit``."""
        exec_id = self._ids[name]
        target = self.active.get(exec_id)
        before_status = self._status_snapshot()
        before_active = dict(self.hook._active_spans)

        self.hook(
            _make_exec_event(
                phase="exit",
                overrides={
                    "pid": _SHARED_PID,
                    "exec_id": exec_id,
                    "exit_code": exit_code,
                    "duration_s": 0.1,
                },
            )
        )

        expected_active = dict(before_active)
        if target is not None:
            assert target.ended is True, "exit must end the matching span"
            assert target.status_ok is (exit_code == 0), (
                "exit status must follow the exit code"
            )
            assert exec_id not in self.hook._active_spans, (
                "exit must remove the matching execution's span"
            )
            expected_active.pop(exec_id, None)
            del self.active[exec_id]
        for span in self.tracer.spans:
            if target is not None and span is target:
                continue
            assert (span.ended, span.status_ok) == before_status[id(span)], (
                "exit must not end or change any other span"
            )
        assert self.hook._active_spans == expected_active, (
            "exit must remove only the matching execution from the active map"
        )

    @rule(
        phase=st.sampled_from(("start", "stdout", "stderr", "exit")),
        line=_LINES,
        exit_code=st.integers(min_value=0, max_value=5),
    )
    def legacy_event(self, phase: str, line: str, exit_code: int) -> None:
        """Dispatch an uncorrelated ``exec_id=None`` event; assert nothing changes."""
        before_spans = list(self.tracer.spans)
        before_events = self._events_snapshot()
        before_status = self._status_snapshot()
        before_active = dict(self.hook._active_spans)

        overrides: dict[str, object] = {"pid": _SHARED_PID, "exec_id": None}
        if phase in _STREAMS:
            overrides["line"] = line
        elif phase == "exit":
            overrides["exit_code"] = exit_code
            overrides["duration_s"] = 0.1
        self.hook(
            _make_exec_event(phase=typ.cast("ExecPhase", phase), overrides=overrides)
        )

        assert list(self.tracer.spans) == before_spans, "legacy events create no span"
        for span in self.tracer.spans:
            assert span.events == before_events[id(span)], (
                "legacy events must record nothing"
            )
            assert (span.ended, span.status_ok) == before_status[id(span)], (
                "legacy events must end or change no span"
            )
        assert self.hook._active_spans == before_active, (
            "legacy events must not alter the active map"
        )

    @invariant()
    def active_map_mirrors_model(self) -> None:
        """Assert the hook's active map matches the model, by object identity."""
        assert self.active.keys() == self.hook._active_spans.keys(), (
            "the active exec_ids must match the model"
        )
        for exec_id, span in self.active.items():
            assert self.hook._active_spans[exec_id] is span, (
                "each active exec_id must map to its own span object"
            )


TestTracingSpanMachine = TracingSpanMachine.TestCase
TestTracingSpanMachine.settings = settings(
    max_examples=40,
    stateful_step_count=20,
    deadline=None,
)
