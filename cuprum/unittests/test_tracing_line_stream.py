"""Tracing projection contract for line-stream lifecycle events.

``TracingHook.record_line_stream_event`` records a bounded attribute mapping
onto the span of the execution that emitted the event. The lifecycle test in
``test_line_stream_observation`` asserts only that two ``cuprum.line_stream``
events arrive; this module pins the projection itself — the complete attribute
mapping, the omission of unset fields, execution correlation, and the paths
that must record nothing at all.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum.events import new_exec_id
from cuprum.line_stream_events import LineStreamEvent, LineStreamPhase
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

LINE_STREAM_EVENT_NAME = "cuprum.line_stream"


def _open_span(traced: Traced, exec_id: ExecId) -> None:
    """Open the execution span that a correlated line-stream event lands on."""
    traced.hook(_make_exec_event(phase="start", overrides=_cat_overrides(exec_id)))


def _close_span(traced: Traced, exec_id: ExecId) -> None:
    """Close the execution span so later events find nothing to record onto."""
    traced.hook(_make_exec_event(phase="exit", overrides=_cat_overrides(exec_id)))


def _projected(span: InMemorySpan) -> list[dict[str, object]]:
    """Return just the attributes of each line-stream event on ``span``."""
    return [
        attributes for name, attributes in span.events if name == LINE_STREAM_EVENT_NAME
    ]


def _assert_projected(
    span: InMemorySpan,
    expected: dict[str, object],
) -> None:
    """Assert exactly one projection landed, carrying exactly ``expected``."""
    projected = _projected(span)
    assert len(projected) == 1, (
        f"one lifecycle event must project exactly one span event, got {projected!r}"
    )
    assert projected[0] == expected, (
        f"the projection must carry the bounded attribute mapping, got {projected[0]!r}"
    )


#: The ``None`` filter is a truth table: a populated field survives, a zero
#: measurement survives because ``0`` is not ``None``, and an unset field is
#: dropped. Each row is one point in that table.
_PROJECTION_ROWS: list[tuple[LineStreamEvent, dict[str, object]]] = [
    (
        LineStreamEvent(
            phase=LineStreamPhase.QUEUE_SATURATED,
            exec_id=new_exec_id(),
            pid=4321,
            stream="stderr",
            sink="queue",
            queue_size=8,
            queue_capacity=8,
            error_type="ValueError",
        ),
        {
            "phase": "queue_saturated",
            "pid": 4321,
            "stream": "stderr",
            "sink": "queue",
            "queue_size": 8,
            "queue_capacity": 8,
            "error_type": "ValueError",
        },
    ),
    (
        LineStreamEvent(
            phase=LineStreamPhase.QUEUE_SATURATED,
            exec_id=new_exec_id(),
            pid=4321,
            stream="stdout",
            sink="queue",
            queue_size=0,
            queue_capacity=0,
        ),
        {
            "phase": "queue_saturated",
            "pid": 4321,
            "stream": "stdout",
            "sink": "queue",
            "queue_size": 0,
            "queue_capacity": 0,
        },
    ),
    (
        LineStreamEvent(
            phase=LineStreamPhase.SPAWNED,
            exec_id=new_exec_id(),
            pid=None,
        ),
        {"phase": "spawned"},
    ),
]


@pytest.mark.parametrize(
    ("event", "expected"),
    _PROJECTION_ROWS,
    ids=["every-field-populated", "zero-measurements-kept", "unset-fields-dropped"],
)
def test_projection_maps_populated_fields_and_drops_unset_ones(
    tracing_hook: Traced,
    event: LineStreamEvent,
    expected: dict[str, object],
) -> None:
    """The projection carries exactly the fields that are not ``None``."""
    _open_span(tracing_hook, event.exec_id)
    tracing_hook.hook.record_line_stream_event(event)
    _assert_projected(tracing_hook.tracer.spans[0], expected)


def test_events_project_onto_the_span_sharing_their_exec_id(
    tracing_hook: Traced,
) -> None:
    """Correlation is by ``exec_id``; a foreign execution records nothing."""
    traced_id = new_exec_id()
    other_id = new_exec_id()
    _open_span(tracing_hook, traced_id)
    _open_span(tracing_hook, other_id)

    tracing_hook.hook.record_line_stream_event(
        LineStreamEvent(
            phase=LineStreamPhase.SPAWNED,
            exec_id=traced_id,
            pid=1,
        )
    )

    traced_span, other_span = tracing_hook.tracer.spans
    _assert_projected(traced_span, {"phase": "spawned", "pid": 1})
    assert _projected(other_span) == [], (
        "an event must project only onto its own execution's span, got "
        f"{_projected(other_span)!r}"
    )


def test_event_without_an_open_span_records_nothing(tracing_hook: Traced) -> None:
    """An unknown execution identifier is dropped instead of raising."""
    tracing_hook.hook.record_line_stream_event(
        LineStreamEvent(
            phase=LineStreamPhase.SPAWNED,
            exec_id=new_exec_id(),
            pid=1,
        )
    )

    assert tracing_hook.tracer.spans == [], (
        "an uncorrelated event must not open or record onto any span, got "
        f"{tracing_hook.tracer.spans!r}"
    )


def test_event_after_the_span_closed_records_nothing(tracing_hook: Traced) -> None:
    """A lifecycle event arriving after ``exit`` is dropped, not appended."""
    exec_id = new_exec_id()
    _open_span(tracing_hook, exec_id)
    _close_span(tracing_hook, exec_id)

    tracing_hook.hook.record_line_stream_event(
        LineStreamEvent(
            phase=LineStreamPhase.TEARDOWN_COMPLETED,
            exec_id=exec_id,
            pid=1,
        )
    )

    span = tracing_hook.tracer.spans[0]
    assert span.ended, "the execution span must have been closed by its exit event"
    assert _projected(span) == [], (
        "no lifecycle event may be recorded after the span closed, got "
        f"{_projected(span)!r}"
    )
