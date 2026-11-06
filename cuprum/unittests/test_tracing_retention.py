"""Tracing projection contract for observe-hook retention samples.

``TracingHook.report_retention`` records a bounded attribute mapping onto the
span of the execution the sample correlates with. This module pins that
projection: the event names, the attributes each sample kind carries, the
distinct event used for the capacity-wait async boundary, and the paths that
must record nothing at all.
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


def test_depth_sample_projects_onto_the_correlated_span(
    tracing_hook: Traced,
) -> None:
    """A depth sample must land on its own execution's span."""
    exec_id = new_exec_id()
    _open_span(tracing_hook, exec_id)

    tracing_hook.hook.report_retention(
        _RetentionSample(
            exec_id=exec_id,
            kind=_RETENTION_DEPTH,
            phase="stdout",
            depth=12,
        )
    )

    assert _projected(tracing_hook.tracer.spans[0], RETENTION_EVENT) == [
        {"kind": "depth", "depth": 12, "phase": "stdout"}
    ], "the depth sample must project its bounded attributes"


def test_prune_sample_projects_the_dropped_counts(tracing_hook: Traced) -> None:
    """A prune sample must carry both the total dropped and the failures."""
    exec_id = new_exec_id()
    _open_span(tracing_hook, exec_id)

    tracing_hook.hook.report_retention(
        _RetentionSample(
            exec_id=exec_id,
            kind=_RETENTION_PRUNE,
            phase="stderr",
            depth=5,
            pruned=4,
            pruned_failures=2,
        )
    )

    assert _projected(tracing_hook.tracer.spans[0], RETENTION_EVENT) == [
        {
            "kind": "prune",
            "depth": 5,
            "phase": "stderr",
            "pruned": 4,
            "pruned_failures": 2,
        }
    ], "the prune sample must project the counts it dropped"


def test_saturation_sample_omits_the_prune_counts(tracing_hook: Traced) -> None:
    """A saturation sample must not carry attributes that do not apply."""
    exec_id = new_exec_id()
    _open_span(tracing_hook, exec_id)

    tracing_hook.hook.report_retention(
        _RetentionSample(
            exec_id=exec_id,
            kind=_RETENTION_SATURATION,
            phase="stdout",
            depth=256,
        )
    )

    assert _projected(tracing_hook.tracer.spans[0], RETENTION_EVENT) == [
        {"kind": "saturation", "depth": 256, "phase": "stdout"}
    ], "a refusal has no prune counts to report"


def test_capacity_wait_projects_its_own_operation_and_outcome(
    tracing_hook: Traced,
) -> None:
    """The wait boundary must be traced with a fixed operation and outcome."""
    exec_id = new_exec_id()
    _open_span(tracing_hook, exec_id)

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
    assert _projected(span, RETENTION_EVENT) == [], (
        "the wait must not be projected as a generic retention sample"
    )
    assert _projected(span, CAPACITY_WAIT_EVENT) == [
        {
            "operation": "observe_task_capacity_wait",
            "outcome": "resumed",
            "wait_seconds": 0.5,
            "depth": 256,
            "phase": "stdout",
        }
    ], "the wait must project its operation, outcome, and duration"


def test_sample_for_an_unknown_execution_records_nothing(
    tracing_hook: Traced,
) -> None:
    """A sample with no open span must be dropped rather than invented."""
    _open_span(tracing_hook, new_exec_id())

    tracing_hook.hook.report_retention(
        _RetentionSample(
            exec_id=new_exec_id(),
            kind=_RETENTION_DEPTH,
            phase="stdout",
            depth=1,
        )
    )

    assert tracing_hook.tracer.spans[0].events == [], (
        "an uncorrelated sample must not be attached to another execution's span"
    )


def test_sample_after_the_span_closed_records_nothing(
    tracing_hook: Traced,
) -> None:
    """A sample arriving after settlement must not touch the closed span."""
    exec_id = new_exec_id()
    _open_span(tracing_hook, exec_id)
    _close_span(tracing_hook, exec_id)

    tracing_hook.hook.report_retention(
        _RetentionSample(
            exec_id=exec_id,
            kind=_RETENTION_DEPTH,
            phase="stdout",
            depth=1,
        )
    )

    assert _projected(tracing_hook.tracer.spans[0], RETENTION_EVENT) == [], (
        "a closed span must not receive retention events"
    )
