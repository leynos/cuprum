"""Span-join tests for control events recorded on an execution span.

A control operation is part of its run's story rather than a run of its own, so
``record_control_events`` records it as a span *event* on the span the run's
``TracingHook`` has already opened. These tests pin that join: the event lands
on the run's own span and on no other, it carries bounded attributes, and an
operation completing after the run has settled is dropped rather than opening a
span that would outlive the execution it describes.

Every event is produced through the production path — ``_start_control`` and
``_complete_control``, the same pair the handle drives — so a change to how a
control event is assembled cannot pass these tests by drifting from what is
asserted here.
"""

from __future__ import annotations

import pytest

from cuprum.adapters.tracing_adapter import (
    InMemoryTracer,
    TracingHook,
    record_control_events,
)
from cuprum.control_events import (
    ControlEvent,
    ControlOperation,
    ControlOutcome,
)
from cuprum.control_observation import (
    _complete_control,
    _ControlTarget,
    _start_control,
    observe_control,
)
from cuprum.events import ExecId, new_exec_id
from cuprum.unittests._adapter_test_support import (
    Traced,
    _cat_overrides,
    _make_exec_event,
    tracing_hook,
)

__all__ = ["tracing_hook"]

_SIGNAL_NUMBER = 15
_PID = 4321

# The bounded attribute set a control span event may carry. Anything outside it
# is either unbounded (a command line, an error string) or a field the event
# does not have, so the exact-set assertions below are the point rather than an
# incidental detail.
_BOUNDED_ATTRIBUTES = frozenset({
    "cuprum.operation",
    "cuprum.outcome",
    "cuprum.duration_s",
    "cuprum.pid",
    "cuprum.signal_number",
})


def _on(
    exec_id: ExecId | None,
    *,
    signal_number: int | None = _SIGNAL_NUMBER,
    pid: int | None = _PID,
) -> _ControlTarget:
    """Aim at the run *exec_id* names, naming this suite's usual child.

    The token is required because every case below has to say what it is
    correlating on: a case that means "no token at all" passes ``None``
    explicitly rather than leaving the target to a default.
    """
    return _ControlTarget(exec_id=exec_id, signal_number=signal_number, pid=pid)


def _perform_control(
    operation: ControlOperation,
    outcome: ControlOutcome,
    *,
    target: _ControlTarget,
) -> ControlEvent | None:
    """Run one control operation through the channel and return what it emitted.

    Returns
    -------
    ControlEvent | None
        The single event the operation emitted, or ``None`` when nothing was
        registered to observe it.
    """
    events: list[ControlEvent] = []
    with observe_control(events.append):
        measurement = _start_control(operation, target=target)
        _complete_control(measurement, outcome)
    return events[0] if events else None


def _open_span(traced: Traced, *, pid: int = _PID, name: str = "cat") -> ExecId:
    """Open one execution span on ``traced`` and return its token."""
    exec_id = new_exec_id()
    traced.hook(_make_exec_event(phase="start", overrides=_cat_overrides(exec_id, pid)))
    return exec_id


def _settle_span(traced: Traced, exec_id: ExecId) -> None:
    """Close the span ``exec_id`` names, as a completed run does."""
    traced.hook(
        _make_exec_event(
            phase="settled",
            overrides={
                **_cat_overrides(exec_id),
                "terminal_outcome": "exit_zero",
            },
        )
    )


def _span_events(traced: Traced, index: int = 0) -> list[tuple[str, dict[str, object]]]:
    """Return the recorded span events of one span, in order."""
    return list(traced.tracer.spans[index].events)


class TestSpanJoin:
    """Recording a control event onto the execution span it acted on."""

    def test_records_the_event_on_the_runs_own_span(self, tracing_hook: Traced) -> None:
        """Land one span event on the span opened for the event's token."""
        exec_id = _open_span(tracing_hook)

        with record_control_events(tracing_hook.hook):
            _perform_control(
                ControlOperation.SIGNAL, ControlOutcome.DELIVERED, target=_on(exec_id)
            )

        assert [name for name, _a in _span_events(tracing_hook)] == [
            "cuprum.control.signal"
        ], "the operation reached the run's span"

    @pytest.mark.parametrize(
        ("operation", "outcome"),
        [
            (ControlOperation.SIGNAL, ControlOutcome.DELIVERED),
            (ControlOperation.TERMINATE, ControlOutcome.ESCALATED),
            (ControlOperation.KILL, ControlOutcome.REFUSED_EXITED),
        ],
        ids=str,
    )
    def test_names_the_event_after_the_operation(
        self,
        tracing_hook: Traced,
        operation: ControlOperation,
        outcome: ControlOutcome,
    ) -> None:
        """Namespace the span event and key it on the closed operation."""
        exec_id = _open_span(tracing_hook)

        with record_control_events(tracing_hook.hook):
            _perform_control(operation, outcome, target=_on(exec_id))

        assert [name for name, _a in _span_events(tracing_hook)] == [
            f"cuprum.control.{operation}"
        ], "the event is named for the operation that produced it"

    def test_carries_only_bounded_attributes(self, tracing_hook: Traced) -> None:
        """Record the closed fields, the duration, and the numeric target."""
        exec_id = _open_span(tracing_hook)

        with record_control_events(tracing_hook.hook):
            _perform_control(
                ControlOperation.SIGNAL, ControlOutcome.DELIVERED, target=_on(exec_id)
            )

        (_name, attributes) = _span_events(tracing_hook)[0]
        assert set(attributes) <= _BOUNDED_ATTRIBUTES, "no unbounded attribute"
        assert attributes["cuprum.operation"] == "signal", "the operation"
        assert attributes["cuprum.outcome"] == "delivered", "the outcome"
        assert isinstance(attributes["cuprum.duration_s"], float), "a measured span"
        assert attributes["cuprum.pid"] == _PID, "the child it was aimed at"
        assert attributes["cuprum.signal_number"] == _SIGNAL_NUMBER, (
            "the signal asked for"
        )

    def test_omits_the_target_fields_the_handle_lacked(
        self,
        tracing_hook: Traced,
    ) -> None:
        """Leave out a PID or signal the producing handle never had."""
        exec_id = _open_span(tracing_hook)

        with record_control_events(tracing_hook.hook):
            _perform_control(
                ControlOperation.TERMINATE,
                ControlOutcome.DELIVERED,
                target=_on(exec_id, signal_number=None, pid=None),
            )

        (_name, attributes) = _span_events(tracing_hook)[0]
        assert attributes == {
            "cuprum.operation": "terminate",
            "cuprum.outcome": "delivered",
            "cuprum.duration_s": attributes["cuprum.duration_s"],
        }, "an absent target field is omitted rather than recorded as null"

    @pytest.mark.parametrize("signal_number", [15, 9], ids=("sigterm", "sigkill"))
    def test_the_signal_reaches_the_span_as_a_plain_int(
        self,
        tracing_hook: Traced,
        signal_number: int,
    ) -> None:
        """Coerce a caller-named signal that arrived as an ``IntEnum``."""
        exec_id = _open_span(tracing_hook)

        with record_control_events(tracing_hook.hook):
            _perform_control(
                ControlOperation.SIGNAL,
                ControlOutcome.DELIVERED,
                target=_on(exec_id, signal_number=signal_number),
            )

        recorded = _span_events(tracing_hook)[0][1]["cuprum.signal_number"]
        assert type(recorded) is int, "the attribute is a plain int, not an enum"
        assert recorded == signal_number, "and it carries the caller's signal"

    def test_each_event_lands_on_the_span_its_own_token_names(self) -> None:
        """Record on the span a token names, leaving unrelated spans alone."""
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
        first = _open_span(Traced(tracer, hook), pid=4321)
        second = _open_span(Traced(tracer, hook), pid=4322)

        with record_control_events(hook):
            _perform_control(
                ControlOperation.KILL, ControlOutcome.DELIVERED, target=_on(second)
            )

        first_span, second_span = tracer.spans
        assert first is not second, "the helper mints a token per call"
        assert first_span.events == [], "the unrelated run's span is untouched"
        assert [name for name, _a in second_span.events] == ["cuprum.control.kill"], (
            "the event reached the run it acted on"
        )

    def test_two_operations_on_one_run_share_its_span(
        self, tracing_hook: Traced
    ) -> None:
        """Append every operation of one run to that run's single span."""
        exec_id = _open_span(tracing_hook)

        with record_control_events(tracing_hook.hook):
            _perform_control(
                ControlOperation.TERMINATE, ControlOutcome.DELIVERED, target=_on(exec_id)
            )
            _perform_control(
                ControlOperation.KILL, ControlOutcome.ESCALATED, target=_on(exec_id)
            )

        assert [name for name, _a in _span_events(tracing_hook)] == [
            "cuprum.control.terminate",
            "cuprum.control.kill",
        ], "one span, two operations, in order"
        assert len(tracing_hook.tracer.spans) == 1, "no span per operation"


class TestDroppedEvents:
    """Refusing to open a span for a control event with no live execution."""

    def test_an_event_after_settlement_finds_no_span(
        self, tracing_hook: Traced
    ) -> None:
        """Drop an operation completing after the run it describes has ended."""
        exec_id = _open_span(tracing_hook)
        _settle_span(tracing_hook, exec_id)

        with record_control_events(tracing_hook.hook):
            _perform_control(
                ControlOperation.KILL, ControlOutcome.DELIVERED, target=_on(exec_id)
            )

        assert not _span_events(tracing_hook), "a settled span receives no events"
        assert len(tracing_hook.tracer.spans) == 1, "no span is opened in its place"

    def test_an_event_without_a_token_finds_no_span(self, tracing_hook: Traced) -> None:
        """Drop an event whose producer carried no correlation token."""
        _open_span(tracing_hook)

        with record_control_events(tracing_hook.hook):
            _perform_control(
                ControlOperation.SIGNAL,
                ControlOutcome.DELIVERED,
                target=_on(None),
            )

        assert not _span_events(tracing_hook), "an uncorrelated event is dropped"

    def test_an_event_for_an_unknown_token_finds_no_span(
        self,
        tracing_hook: Traced,
    ) -> None:
        """Drop an event naming a run this hook never observed."""
        _open_span(tracing_hook)

        with record_control_events(tracing_hook.hook):
            _perform_control(
                ControlOperation.SIGNAL,
                ControlOutcome.DELIVERED,
                target=_on(new_exec_id()),
            )

        assert not _span_events(tracing_hook), "nothing was recorded"
        assert len(tracing_hook.tracer.spans) == 1, "and nothing was opened"


def test_the_join_is_a_separate_registration(tracing_hook: Traced) -> None:
    """Leave an unjoined run's span free of control events."""
    exec_id = _open_span(tracing_hook)

    _perform_control(ControlOperation.KILL, ControlOutcome.DELIVERED, target=_on(exec_id))

    assert not _span_events(tracing_hook), (
        "a caller who never registered the join sees no control events"
    )


def test_the_emitted_event_is_the_one_the_join_recorded(
    tracing_hook: Traced,
) -> None:
    """Assert the recorded artefact, not only the fact that something arrived."""
    exec_id = _open_span(tracing_hook)
    emitted: list[ControlEvent] = []

    with (
        record_control_events(tracing_hook.hook),
        observe_control(emitted.append),
    ):
        _perform_control(
            ControlOperation.KILL,
            ControlOutcome.REFUSED_EXITED,
            target=_on(exec_id),
        )

    assert len(emitted) == 1, "the channel emitted exactly one event"
    (name, attributes) = _span_events(tracing_hook)[0]
    source = emitted[0]
    assert name == f"cuprum.control.{source.operation}", (
        "the span event names the source"
    )
    assert attributes["cuprum.outcome"] == str(source.outcome), "the same outcome"
    assert attributes["cuprum.pid"] == source.pid, "the same child"
    assert attributes["cuprum.signal_number"] == source.signal_number, "the same signal"
