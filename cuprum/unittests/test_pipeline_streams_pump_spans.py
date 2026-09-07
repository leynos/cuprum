"""Executor-seam tests for opt-in Rust-pump hop spans."""

from __future__ import annotations

import asyncio
import threading
import typing as typ
from unittest import mock

import pytest

from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.pump_span_events import (
    NATIVE_PUMP_BUFFER_SIZE,
    PUMP_HOP_BUFFER_SIZE_ATTRIBUTE,
    PUMP_HOP_OPERATION_ATTRIBUTE,
    PUMP_HOP_OUTCOME_ATTRIBUTE,
    PUMP_HOP_SPAN_NAME,
    PUMP_HOP_TOTAL_BYTES_ATTRIBUTE,
    PumpHopOutcome,
)
from cuprum.pump_span_observation import observe_pump_span
from cuprum.unittests._rust_pump_test_helpers import (
    DECLINE_PATHS,
    PumpTransfer,
    cancel_fake_pump,
    run_fake_pump,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def test_successful_executor_hop_opens_and_ends_one_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful fast-path transfer records its bounded completion facts."""
    tracer = InMemoryTracer()

    def pump(reader_fd: int, writer_fd: int) -> int:
        """Return the fixed byte count the executor callback must record."""
        del reader_fd, writer_fd
        return 23

    with observe_pump_span(tracer):
        asyncio.run(run_fake_pump(pump, events=[], monkeypatch=monkeypatch))

    assert len(tracer.spans) == 1, f"expected one hop span, found {tracer.spans}"
    span = tracer.spans[0]
    assert span.name == PUMP_HOP_SPAN_NAME, f"unexpected span name {span.name!r}"
    assert span.ended is True, "successful hop span must end"
    assert span.status_ok is True, "successful hop span must be marked ok"
    assert span.attributes == {
        PUMP_HOP_OPERATION_ATTRIBUTE: "rust_pump",
        PUMP_HOP_BUFFER_SIZE_ATTRIBUTE: NATIVE_PUMP_BUFFER_SIZE,
        PUMP_HOP_OUTCOME_ATTRIBUTE: PumpHopOutcome.SUCCEEDED,
        PUMP_HOP_TOTAL_BYTES_ATTRIBUTE: 23,
    }, f"unexpected bounded span attributes {span.attributes}"


@pytest.mark.parametrize(
    "trigger",
    [trigger for _path_id, trigger, _reason in DECLINE_PATHS],
    ids=[path_id for path_id, _trigger, _reason in DECLINE_PATHS],
)
def test_declined_paths_open_no_executor_hop_span(
    monkeypatch: pytest.MonkeyPatch,
    trigger: cabc.Callable[[pytest.MonkeyPatch], None],
) -> None:
    """Fast-path declines must not be represented as executor hop spans."""
    tracer = InMemoryTracer()

    with observe_pump_span(tracer):
        trigger(monkeypatch)

    assert tracer.spans == [], f"declined path must not open a span: {tracer.spans}"


def test_failed_executor_hop_ends_without_success_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uncancelled worker failure closes one failed span without byte data."""
    tracer = InMemoryTracer()

    def pump(reader_fd: int, writer_fd: int) -> int:
        """Fail after executor submission."""
        del reader_fd, writer_fd
        msg = "worker failed"
        raise OSError(msg)

    with observe_pump_span(tracer), pytest.raises(OSError, match="worker failed"):
        asyncio.run(run_fake_pump(pump, events=[], monkeypatch=monkeypatch))

    assert len(tracer.spans) == 1, f"failed hop must create one span: {tracer.spans}"
    span = tracer.spans[0]
    assert span.ended is True, "failed hop span must end"
    assert span.attributes[PUMP_HOP_OUTCOME_ATTRIBUTE] is PumpHopOutcome.FAILED
    assert PUMP_HOP_TOTAL_BYTES_ATTRIBUTE not in span.attributes
    assert span.status_ok is None, "failed hop span must not be marked ok"


def test_rejected_executor_submission_ends_failed_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected submission closes the already-open hop span as failed."""
    tracer = InMemoryTracer()

    def pump(_reader_fd: int, _writer_fd: int) -> int:
        """Provide the worker entry point that submission rejects."""
        return 0

    async def reject_submission() -> None:
        """Reject executor acceptance after the hop span opens."""
        loop = asyncio.get_running_loop()
        with (
            mock.patch.object(
                loop,
                "run_in_executor",
                side_effect=RuntimeError("executor rejected the worker"),
            ),
            pytest.raises(RuntimeError, match="executor rejected"),
        ):
            await run_fake_pump(pump, events=[], monkeypatch=monkeypatch)

    with observe_pump_span(tracer):
        asyncio.run(reject_submission())

    assert len(tracer.spans) == 1, f"rejected hop must create one span: {tracer.spans}"
    span = tracer.spans[0]
    assert span.ended is True, "rejected hop span must end"
    assert span.attributes[PUMP_HOP_OUTCOME_ATTRIBUTE] is PumpHopOutcome.FAILED
    assert PUMP_HOP_TOTAL_BYTES_ATTRIBUTE not in span.attributes
    assert span.status_ok is None, "rejected hop span must not be marked ok"


@pytest.mark.parametrize(
    ("should_fail", "expected_outcome"),
    [
        (False, PumpHopOutcome.CANCELLED),
        (True, PumpHopOutcome.FAILED_AFTER_CANCEL),
    ],
    ids=("clean-worker", "failing-worker"),
)
def test_cancelled_executor_hop_ends_with_expected_outcome(
    *,
    should_fail: bool,
    expected_outcome: PumpHopOutcome,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled hops retain worker ownership until the expected outcome is set."""
    transfer = PumpTransfer([], threading.Event(), threading.Event())
    tracer = InMemoryTracer()

    def pump(reader_fd: int, writer_fd: int) -> int:
        """Wait for cancellation, then return or fail as configured."""
        del reader_fd, writer_fd
        transfer.started.set()
        assert transfer.release.wait(5.0), "harness did not release the worker"
        transfer.events.append("worker_returned")
        if should_fail:
            msg = "worker failed after cancellation"
            raise OSError(msg)
        return 0

    with observe_pump_span(tracer):
        asyncio.run(cancel_fake_pump(transfer, pump, monkeypatch=monkeypatch))

    span = tracer.spans[0]
    assert span.ended is True, "cancelled hop span must end"
    assert span.attributes[PUMP_HOP_OUTCOME_ATTRIBUTE] == expected_outcome, (
        f"unexpected cancellation outcome {span.attributes}"
    )
    assert span.status_ok is None, "cancelled hop must not be marked ok"
    assert transfer.events.index("worker_returned") < transfer.events.index(
        "restored"
    ), f"worker must return before descriptor restore: {transfer.events}"
