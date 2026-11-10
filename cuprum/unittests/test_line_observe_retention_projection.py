"""Retention projection through both bundled adapters on one real run.

``MetricsHook`` and ``TracingHook`` are ordinary observe hooks, so a run that
installs both feeds one retention recorder and each adapter projects the same
samples its own way. The unit tests pin each projection in isolation; this case
pins them together, against a real child flooding output behind a gated
observer. It witnesses the whole chain — admission refuses a line, the line
waits, a prune drops settled tasks, and the wait resumes — rather than a
hand-built sample, and it holds the span to the once-per-execution bound the
per-line path would otherwise break.
"""

from __future__ import annotations

import asyncio

import pytest

from cuprum import sh
from cuprum.adapters.metrics_adapter import MetricsHook
from cuprum.adapters.tracing_adapter import TracingHook
from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.context import ScopeConfig, scoped
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector
from cuprum.unittests.test_line_observe_support import (
    _chatty_command,
    _GatedHook,
    _output_options,
    _RetentionProbe,
)
from cuprum.unittests.test_retention_metrics import (
    DEPTH,
    PRUNED,
    SATURATIONS,
    WAIT_SECONDS,
)
from cuprum.unittests.test_tracing_retention import (
    CAPACITY_WAIT_EVENT,
    RETENTION_EVENT,
)

_CAPTURE_MODES = [
    pytest.param(True, id="captured"),
    pytest.param(False, id="uncaptured"),
]

#: Flood size large enough that the run keeps pruning after the gate opens.
_LINE_COUNT = 5_000

#: How long the reader may take to reach the retention ceiling.
_SATURATION_TIMEOUT_SECONDS = 20.0

#: How long the whole observed run may take once the gate opens.
_RUN_TIMEOUT_SECONDS = 60.0


@pytest.mark.parametrize("capture", _CAPTURE_MODES)
def test_a_real_run_projects_retention_to_metrics_and_spans(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """A saturated real run must reach both adapters with every traced kind."""
    asyncio.run(_projection_case(monkeypatch, capture))


async def _projection_case(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """Run the flooding child with both adapters installed and check them."""
    cmd, catalogue = _chatty_command(_LINE_COUNT)
    hook = _GatedHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)
    collector = RecordingCollector()
    tracer = InMemoryTracer()

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(hook),
        sh.observe(MetricsHook(collector)),
        sh.observe(TracingHook(tracer)),
    ):
        run_task = asyncio.create_task(cmd.run(output=_output_options(capture)))
        await asyncio.wait_for(
            probe.saturated.wait(),
            timeout=_SATURATION_TIMEOUT_SECONDS,
        )
        hook.gate.set()
        await asyncio.wait_for(run_task, timeout=_RUN_TIMEOUT_SECONDS)

    _assert_metrics_recorded(collector)
    _assert_span_recorded(tracer)


def _assert_metrics_recorded(collector: RecordingCollector) -> None:
    """Check the run reached every retention metric family it must."""
    assert SATURATIONS in collector.counter_names(), (
        "a refused admission must reach the saturation counter"
    )
    pruned = sum(value for name, value, _labels in collector.counters if name == PRUNED)
    assert pruned > 0, (
        "the run must have pruned settled tasks for the prune counter to move"
    )
    histogram_names = {name for name, _value, _labels in collector.histograms}
    assert DEPTH in histogram_names, "the prune path must observe the depth"
    assert WAIT_SECONDS in histogram_names, (
        "the suspended line must observe its capacity-wait duration"
    )


def _assert_span_recorded(tracer: InMemoryTracer) -> None:
    """Check the child's span carries one event of each traced kind."""
    assert len(tracer.spans) == 1, (
        f"the single observed child must open one span, got {len(tracer.spans)}"
    )
    span = tracer.spans[0]
    names = [name for name, _attributes in span.events]
    assert names.count(CAPACITY_WAIT_EVENT) == 1, (
        "every deferred line waits, but the span records the wait once"
    )
    assert names.count(RETENTION_EVENT) == 1, (
        "the run prunes repeatedly, but the span records a prune once"
    )
    wait_attributes = next(
        attributes for name, attributes in span.events if name == CAPACITY_WAIT_EVENT
    )
    assert wait_attributes["outcome"] == "resumed", (
        "the observed wait resumed; no cancellation ended it"
    )
    assert span.ended, "the settled event must end the child's span"
