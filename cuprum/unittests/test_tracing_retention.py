"""Tracing projection contract for observe-hook retention samples.

``TracingHook.report_retention`` attaches a retention sample to the span of the
execution it correlates with. This module pins which samples qualify: the
capacity-wait async boundary and a prune that actually dropped settled tasks.
Depth and saturation samples run on the per-line path, so tracing them would
grow the span with the line count; the tests below witness that they record
nothing beside samples that do, on the same span. The qualifying kinds run on
that path too, so each is recorded at most once per execution span, and the
last tests witness that bound rather than only the filter.
"""

from __future__ import annotations

import typing as typ

from cuprum._retention_telemetry import (
    _RETENTION_CAPACITY_WAIT,
    _RETENTION_DEPTH,
    _RETENTION_PRUNE,
    _RETENTION_SATURATION,
    _RetentionSample,
)
from cuprum.events import TerminalOutcome, new_exec_id
from cuprum.unittests._adapter_test_support import (
    Traced,
    _cat_overrides,
    _make_exec_event,
    tracing_hook,
)

if typ.TYPE_CHECKING:
    from cuprum.adapters.tracing_memory import InMemorySpan
    from cuprum.events import ExecId

__all__ = ["tracing_hook"]

RETENTION_EVENT = "cuprum.observe_task_retention"
CAPACITY_WAIT_EVENT = "cuprum.observe_task_capacity_wait"


def _open_span(traced: Traced, exec_id: ExecId) -> None:
    """Open the execution span a correlated retention sample lands on."""
    traced.hook(_make_exec_event(phase="start", overrides=_cat_overrides(exec_id)))


def _close_span(traced: Traced, exec_id: ExecId) -> None:
    """Settle the execution span so later samples find nothing to record onto."""
    traced.hook(
        _make_exec_event(
            phase="settled",
            overrides={
                **_cat_overrides(exec_id),
                "terminal_outcome": TerminalOutcome.EXIT_ZERO,
            },
        )
    )


def _projected(span: InMemorySpan, name: str) -> list[dict[str, object]]:
    """Return the attributes of each event called ``name`` on ``span``."""
    return [attributes for event, attributes in span.events if event == name]


def _prune_sample(
    exec_id: ExecId,
    *,
    pruned: int,
    pruned_failures: int = 0,
    phase: str | None = "stderr",
) -> _RetentionSample:
    """Build a prune sample that dropped ``pruned`` settled tasks."""
    return _RetentionSample(
        exec_id=exec_id,
        kind=_RETENTION_PRUNE,
        phase=phase,
        depth=5,
        pruned=pruned,
        pruned_failures=pruned_failures,
    )


def test_only_backpressure_explaining_samples_reach_the_span(
    tracing_hook: Traced,
) -> None:
    """Per-line samples must record nothing; dropping prunes and waits must."""
    exec_id = new_exec_id()
    _open_span(tracing_hook, exec_id)
    for sample in (
        _RetentionSample(
            exec_id=exec_id, kind=_RETENTION_DEPTH, phase="stdout", depth=12
        ),
        _RetentionSample(
            exec_id=exec_id, kind=_RETENTION_SATURATION, phase="stdout", depth=256
        ),
        _prune_sample(exec_id, pruned=0),
    ):
        tracing_hook.hook.report_retention(sample)
    tracing_hook.hook.report_retention(
        _prune_sample(exec_id, pruned=4, pruned_failures=2)
    )
    tracing_hook.hook.report_retention(
        _RetentionSample(
            exec_id=exec_id,
            kind=_RETENTION_CAPACITY_WAIT,
            phase="stdout",
            depth=256,
            wait_seconds=0.5,
            outcome="resumed",
        )
    )

    span = tracing_hook.tracer.spans[0]
    assert _projected(span, RETENTION_EVENT) == [
        {
            "kind": "prune",
            "depth": 5,
            "phase": "stderr",
            "pruned": 4,
            "pruned_failures": 2,
        }
    ], "only the prune that dropped settled tasks may project onto the span"
    assert _projected(span, CAPACITY_WAIT_EVENT) == [
        {
            "operation": "observe_task_capacity_wait",
            "outcome": "resumed",
            "wait_seconds": 0.5,
            "depth": 256,
            "phase": "stdout",
        }
    ], "the wait must project its operation, outcome, and duration"


def test_phaseless_samples_omit_the_phase_and_unknown_outcome(
    tracing_hook: Traced,
) -> None:
    """A hand-built sample has no phase, and an unrecorded outcome is named."""
    exec_id = new_exec_id()
    _open_span(tracing_hook, exec_id)
    tracing_hook.hook.report_retention(_prune_sample(exec_id, pruned=1, phase=None))
    tracing_hook.hook.report_retention(
        _RetentionSample(
            exec_id=exec_id,
            kind=_RETENTION_CAPACITY_WAIT,
            depth=1,
            wait_seconds=0.25,
        )
    )

    span = tracing_hook.tracer.spans[0]
    assert _projected(span, RETENTION_EVENT) == [
        {"kind": "prune", "depth": 5, "pruned": 1, "pruned_failures": 0}
    ], "a phaseless prune must not invent a phase attribute"
    assert _projected(span, CAPACITY_WAIT_EVENT) == [
        {
            "operation": "observe_task_capacity_wait",
            "outcome": "unknown",
            "wait_seconds": 0.25,
            "depth": 1,
        }
    ], "a wait with no recorded outcome must be labelled unknown"


def test_sample_for_an_unknown_execution_records_nothing(
    tracing_hook: Traced,
) -> None:
    """A sample with no open span must be dropped rather than invented."""
    _open_span(tracing_hook, new_exec_id())
    stranger = new_exec_id()

    tracing_hook.hook.report_retention(_prune_sample(stranger, pruned=1))
    tracing_hook.hook.report_retention(
        _RetentionSample(
            exec_id=stranger,
            kind=_RETENTION_CAPACITY_WAIT,
            wait_seconds=0.1,
            outcome="resumed",
        )
    )

    span = tracing_hook.tracer.spans[0]
    assert span.events == [], (
        "an uncorrelated sample must not be attached to another execution's span"
    )


def test_sample_after_the_span_closed_records_nothing(
    tracing_hook: Traced,
) -> None:
    """A sample arriving after settlement must not touch the closed span."""
    exec_id = new_exec_id()
    _open_span(tracing_hook, exec_id)
    _close_span(tracing_hook, exec_id)

    tracing_hook.hook.report_retention(_prune_sample(exec_id, pruned=1))
    tracing_hook.hook.report_retention(
        _RetentionSample(
            exec_id=exec_id,
            kind=_RETENTION_CAPACITY_WAIT,
            wait_seconds=0.1,
            outcome="cancelled",
        )
    )

    span = tracing_hook.tracer.spans[0]
    for name in (RETENTION_EVENT, CAPACITY_WAIT_EVENT):
        assert _projected(span, name) == [], (
            "a closed span must not receive retention events"
        )


def _capacity_wait_sample(
    exec_id: ExecId,
    *,
    outcome: str = "resumed",
) -> _RetentionSample:
    """Build a capacity-wait sample for the held line reader."""
    return _RetentionSample(
        exec_id=exec_id,
        kind=_RETENTION_CAPACITY_WAIT,
        phase="stdout",
        depth=256,
        wait_seconds=0.1,
        outcome=outcome,
    )


def test_repeated_waits_and_prunes_record_one_event_each(
    tracing_hook: Traced,
) -> None:
    """A run held at the ceiling must not grow its span with lines deferred."""
    exec_id = new_exec_id()
    _open_span(tracing_hook, exec_id)
    for _ in range(20):
        tracing_hook.hook.report_retention(_prune_sample(exec_id, pruned=2))
        tracing_hook.hook.report_retention(_capacity_wait_sample(exec_id))

    span = tracing_hook.tracer.spans[0]
    assert len(_projected(span, RETENTION_EVENT)) == 1, (
        "repeated prunes must record one retention event, not one per line"
    )
    assert len(_projected(span, CAPACITY_WAIT_EVENT)) == 1, (
        "repeated capacity waits must record one wait event, not one per line"
    )
    assert len(span.events) == 2, "the span must hold a constant number of events"


def test_the_once_bound_holds_per_execution_span(tracing_hook: Traced) -> None:
    """A second execution span must still receive its own first events."""
    for _ in range(2):
        exec_id = new_exec_id()
        _open_span(tracing_hook, exec_id)
        tracing_hook.hook.report_retention(_prune_sample(exec_id, pruned=1))
        tracing_hook.hook.report_retention(
            _capacity_wait_sample(exec_id, outcome="cancelled")
        )

    spans = tracing_hook.tracer.spans
    assert len(spans) == 2, "each execution must open its own span"
    for span in spans:
        assert len(_projected(span, RETENTION_EVENT)) == 1, (
            "the bound must not leak between executions"
        )
        assert len(_projected(span, CAPACITY_WAIT_EVENT)) == 1, (
            "a later execution must still record its own wait"
        )
