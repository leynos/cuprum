"""Conformance evidence linking Loom's lifecycle model to Python pumping.

The bounded Loom model cannot execute asyncio, the GIL, or real descriptors.
These tests retain the complementary integration evidence by driving the public
Rust-pump cancellation path with deterministic events and comparing its trace
to the mapping in ``docs/design-loom-native-pump-model.md``.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import pytest

from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.pump_span_events import PumpHopOutcome
from tests.behaviour._rust_pump_span_support import run_cancelled_hop


def test_cancelled_pump_trace_matches_the_loom_transition_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup begins, worker settles, spans close, then state is restored.

    The existing cancellation and FD-lifecycle regression modules remain the
    detailed integration specifications. This conformance test is their narrow
    correspondence check against the Loom inventory, using ``observe_pump``
    and ``observe_pump_span`` through ``run_cancelled_hop``.
    """
    tracer = InMemoryTracer()

    trace = run_cancelled_hop(tracer, monkeypatch)

    assert trace.index("worker_returned") < trace.index("span_ended"), (
        "the completion callback must observe worker settlement before ending spans"
    )
    assert trace.index("span_ended") < trace.index("restored"), (
        "blocking-mode restoration must follow terminal observation"
    )
    assert len(tracer.spans) == 1, "one native-pump lifecycle creates one hop span"
    span = tracer.spans[0]
    assert span.ended is True, "the terminal span must close"
    assert span.attributes["cuprum.outcome"] == PumpHopOutcome.CANCELLED, (
        "the cancelled hop must record the cancelled outcome"
    )
