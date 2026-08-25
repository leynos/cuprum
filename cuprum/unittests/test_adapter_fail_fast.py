"""How each shipped adapter renders the pipeline fail-fast decision.

The decision reaches every registered hook as one ``pipeline_fail_fast``
event; what the three adapters are each meant to do with it differs, and the
differences are the point:

- metrics turn it into a single low-cardinality counter, deliberately dropping
  the identifying fields that would make the series unbounded;
- tracing keeps those fields, attaching them to the failing stage's open span
  rather than opening one of its own; and
- logging renders them into a message an operator can read at ``WARNING``.

The adapters' fallback contracts are asserted alongside, because adding a
recognized phase must not soften the handling of an unrecognized one.
"""

from __future__ import annotations

import logging
import typing as typ

import pytest

from cuprum.adapters.logging_adapter import LogLevels, structured_logging_hook
from cuprum.adapters.metrics_adapter import (
    InMemoryMetrics,
    MetricsHook,
    _UnhandledMetricsPhaseError,
)
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook
from cuprum.events import new_exec_id
from cuprum.unittests._adapter_test_support import (
    _LabelRecordingCollector,
    _make_exec_event,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecId, ExecPhase

_FAIL_FAST_COUNTER = "cuprum_pipeline_fail_fast_total"
_FAIL_FAST_SPAN_EVENT = "cuprum.pipeline_fail_fast"


def _fail_fast_event(
    *,
    exec_id: ExecId | None = None,
    overrides: cabc.Mapping[str, object] | None = None,
) -> ExecEvent:
    """Build a representative fail-fast event for a stage 1-of-4 failure."""
    values: dict[str, object] = {
        "exec_id": exec_id if exec_id is not None else new_exec_id(),
        "exit_code": 3,
        "duration_s": 0.25,
        "stage_index": 1,
        "stage_count": 4,
        "tags": {},
        "project": "pipe",
    }
    values.update(overrides or {})
    return _make_exec_event(phase="pipeline_fail_fast", overrides=values)


class TestMetricsFailFast:
    """One bounded counter, and nothing that could make it unbounded."""

    def test_the_counter_increments_once(self) -> None:
        """A fail-fast event increments the fail-fast counter exactly once.

        A pipeline torn down early is a distinct outcome from a command that
        merely exited non-zero, so it gets its own series rather than being
        inferred from ``cuprum_failures_total``.
        """
        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        hook(_fail_fast_event())

        assert metrics.counters == {_FAIL_FAST_COUNTER: 1.0}, (
            f"expected one fail-fast increment and nothing else, "
            f"found {metrics.counters!r}"
        )

    def test_repeated_events_accumulate(self) -> None:
        """Two fail-fast pipelines count as two, not as one or as four.

        Guards the increment value itself: a counter fed the event's stage
        count or exit code instead of 1.0 would still look plausible on a
        single event.
        """
        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        hook(_fail_fast_event())
        hook(_fail_fast_event())

        assert metrics.counters[_FAIL_FAST_COUNTER] == 2.0, (
            f"two fail-fast events must count two, "
            f"found {metrics.counters[_FAIL_FAST_COUNTER]!r}"
        )

    def test_the_labels_stay_low_cardinality(self) -> None:
        """Only ``program`` and ``project`` are labels; the rest are not.

        ``exec_id`` is unique per execution, and stage index and exit code
        multiply series for no aggregate a dashboard needs. Any of them
        becoming a label would give the counter unbounded or needlessly wide
        cardinality, which is the failure this test exists to catch — so it
        asserts on the exact label set rather than merely on what is present.
        """
        collector = _LabelRecordingCollector()
        hook = MetricsHook(collector)
        event = _fail_fast_event(overrides={"tags": {"project": "untrusted"}})

        hook(event)

        assert [name for name, _, _ in collector.calls] == [_FAIL_FAST_COUNTER], (
            f"exactly one metric must be emitted, found {collector.calls!r}"
        )
        (labels,) = collector.labels
        assert labels == {"program": "cat", "project": "pipe"}, (
            f"the fail-fast counter must carry only the bounded program and "
            f"project labels, found {labels!r}"
        )
        forbidden = {
            str(event.exec_id),
            str(event.stage_index),
            str(event.stage_count),
            str(event.exit_code),
        }
        assert not forbidden & set(labels.values()), (
            f"no identifying field may appear as a label value, found {labels!r}"
        )

    def test_an_unrecognized_phase_still_raises(self) -> None:
        """Adding a recognized phase must not soften the fail-closed default.

        The metrics hook rejects phases it has no semantics for, so a phase
        added to the event contract without a metrics decision is caught rather
        than silently uncounted. Recognizing ``pipeline_fail_fast`` must not
        turn that into a catch-all that accepts anything.
        """
        hook = MetricsHook(InMemoryMetrics())

        with pytest.raises(_UnhandledMetricsPhaseError, match="future_phase"):
            hook(_make_exec_event(phase=typ.cast("ExecPhase", "future_phase")))


class TestTracingFailFast:
    """The decision annotates the failing stage's span, and opens none."""

    @staticmethod
    def _traced(hook: TracingHook, tracer: InMemoryTracer) -> ExecId:
        """Open a span for one execution and return its correlation token."""
        exec_id = new_exec_id()
        hook(_make_exec_event(phase="start", overrides={"exec_id": exec_id}))
        assert len(tracer.spans) == 1, "the start event must open exactly one span"
        return exec_id

    def test_the_event_lands_on_the_matching_span(self) -> None:
        """The span found by ``exec_id`` gets the event, with its fields.

        This is what the correlation token buys: the fail-fast decision is
        recorded on the span of the stage it is about, so a trace shows the
        teardown in context rather than as an orphan.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
        exec_id = self._traced(hook, tracer)

        hook(_fail_fast_event(exec_id=exec_id))

        span = tracer.spans[0]
        assert [name for name, _ in span.events] == [_FAIL_FAST_SPAN_EVENT], (
            f"the span must carry exactly the fail-fast event, found {span.events!r}"
        )
        _, attributes = span.events[0]
        assert attributes == {
            "stage_index": 1,
            "stage_count": 4,
            "exit_code": 3,
            "duration_s": 0.25,
        }, f"the span event must carry the decision's fields, found {attributes!r}"

    def test_no_span_is_started_or_ended(self) -> None:
        """The stage's own ``exit`` event still owns the span's lifecycle.

        A span of its own would have no duration to report and would need a
        second key to be joined back to the stage; ending the stage's span
        early would truncate it and drop the exit status.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
        exec_id = self._traced(hook, tracer)

        hook(_fail_fast_event(exec_id=exec_id))

        assert len(tracer.spans) == 1, (
            f"no additional span may be started, found {tracer.spans!r}"
        )
        assert not tracer.spans[0].ended, (
            "the stage's span must stay open for its own exit event to close"
        )

    def test_an_uncorrelatable_event_is_dropped(self) -> None:
        """Without a usable token the event is dropped, not misattributed.

        Attaching it to whichever span happened to be open would put one
        pipeline's teardown on another execution's trace, which is worse than
        losing the annotation.
        """
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
        self._traced(hook, tracer)

        # No token at all, then a token no open span was ever keyed by.
        for exec_id in (None, new_exec_id()):
            hook(
                _make_exec_event(
                    phase="pipeline_fail_fast",
                    overrides={
                        "exec_id": exec_id,
                        "exit_code": 3,
                        "duration_s": 0.25,
                        "stage_index": 1,
                        "stage_count": 4,
                    },
                ),
            )

        assert tracer.spans[0].events == [], (
            f"an event with no correlatable token must be dropped, "
            f"found {tracer.spans[0].events!r}"
        )


class TestLoggingFailFast:
    """The decision is legible at a level a default configuration shows."""

    def test_it_logs_at_warning_with_the_decision(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The message names the stage, the width, the exit code, and the time.

        The adapter's unhandled-phase fallback renders only the program, which
        is the one part of a fail-fast event that says nothing, and logs at
        DEBUG where a default configuration would not show it at all.
        """
        hook = structured_logging_hook()
        caplog.set_level(logging.DEBUG, logger="cuprum.exec")

        hook(_fail_fast_event())

        (record,) = [
            record
            for record in caplog.records
            if vars(record).get("cuprum_phase") == "pipeline_fail_fast"
        ]
        assert record.levelno == logging.WARNING, (
            f"a fail-fast decision must log at WARNING, found {record.levelname}"
        )
        assert record.getMessage() == (
            "cuprum.pipeline_fail_fast program=cat stage_index=1 "
            "stage_count=4 exit_code=3 duration_s=0.250000"
        ), f"the message must render the decision, found {record.getMessage()!r}"

    def test_the_extras_carry_the_stage_fields(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Structured consumers get the fields without parsing the message."""
        hook = structured_logging_hook()
        caplog.set_level(logging.DEBUG, logger="cuprum.exec")

        event = _fail_fast_event()
        hook(event)

        fields = vars(caplog.records[0])
        assert (fields["cuprum_stage_index"], fields["cuprum_stage_count"]) == (1, 4), (
            "the log extras must project the stage position and pipeline width"
        )
        assert fields["cuprum_exec_id"] == event.exec_id, (
            "the log extras must preserve the execution correlation token"
        )

    def test_the_level_is_configurable(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``LogLevels.fail_fast_level`` selects the level, not a hard-coded one."""
        hook = structured_logging_hook(levels=LogLevels(fail_fast_level=logging.ERROR))
        caplog.set_level(logging.DEBUG, logger="cuprum.exec")

        hook(_fail_fast_event())

        assert caplog.records[0].levelno == logging.ERROR, (
            f"the configured level must be used, found {caplog.records[0].levelname}"
        )

    def test_sensitive_values_stay_out_of_fail_fast_projections(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Warning logs, traces, and metrics retain only fail-fast decision data."""
        sensitive_value = "pipeline-secret"
        event = _fail_fast_event(
            overrides={
                "argv": ("cat", f"--token={sensitive_value}"),
                "tags": {"project": "pipe", "token": sensitive_value},
            }
        )
        logger_hook = structured_logging_hook()
        caplog.set_level(logging.DEBUG, logger="cuprum.exec")

        logger_hook(event)

        tracer = InMemoryTracer()
        trace_hook = TracingHook(tracer)
        trace_hook(
            _make_exec_event(phase="start", overrides={"exec_id": event.exec_id})
        )
        trace_hook(event)
        collector = _LabelRecordingCollector()
        MetricsHook(collector)(event)

        projections = {
            "log": vars(caplog.records[0]),
            "span": tracer.spans[0].events,
            "metrics": collector.calls,
        }
        assert sensitive_value not in repr(projections), (
            "fail-fast telemetry and its snapshot-ready payload must not expose "
            "argv or arbitrary tags"
        )
        assert "cuprum_argv" not in projections["log"], (
            "fail-fast logs must not project the raw argument vector"
        )
        assert "cuprum_tags" not in projections["log"], (
            "fail-fast logs must not project arbitrary caller tags"
        )
