"""Public pipeline ordering and failure behaviour for bounded delivery."""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest

import cuprum._bounded_delivery as bounded_delivery
from cuprum import ScopeConfig, scoped, sh
from cuprum.echo_events import EchoErrorCategory, EchoStream, RelayFallback
from cuprum.sh import (
    BoundedDelivery,
    ExecutionContext,
    Pipeline,
    PipelineResult,
    RunOutputOptions,
    SafeCmd,
)
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum._bounded_delivery import _DeliveryPayload, _DeliveryWorker
    from cuprum._stream_drain_state import _RelayDiagnostics
    from cuprum.program import Program


class _FailingPipelineSink(io.StringIO):
    """Fail one presentation operation for public pipeline coverage."""

    def __init__(self, failure_site: str) -> None:
        """Select the failing destination operation."""
        super().__init__()
        self.failure_site = failure_site

    def write(self, text: str, /) -> int:
        """Fail the selected write or retain the echoed text."""
        if self.failure_site == "write":
            raise OSError
        return super().write(text)

    def flush(self) -> None:
        """Fail the selected flush or preserve StringIO semantics."""
        if self.failure_site == "flush":
            raise OSError
        super().flush()


def _pipeline() -> tuple[Program, Pipeline, SafeCmd]:
    """Build a two-stage Python pipeline and return its catalogue program."""
    catalogue, program = python_catalogue()
    python = sh.make(program, catalogue=catalogue)
    producer = python(
        "-c",
        "import sys; print('to-child'); print('producer-error', file=sys.stderr)",
    )
    consumer = python("-c", "import sys; sys.stdout.write(sys.stdin.read())")
    return program, producer | consumer, producer


def _run_pipeline(
    pipeline: Pipeline,
    *,
    output: RunOutputOptions,
    context: ExecutionContext,
) -> PipelineResult:
    """Run one pipeline under the current event-loop boundary."""
    return asyncio.run(pipeline.run(output=output, context=context))


def test_pipeline_sink_writes_follow_actual_enqueue_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public pipeline presentation matches the shared worker's FIFO order."""
    program, pipeline, _producer = _pipeline()
    sink = io.StringIO()
    queued_values: list[str] = []
    original_enqueue = bounded_delivery._DeliveryWorker.enqueue

    def record_enqueue(
        worker: _DeliveryWorker,
        stream: EchoStream,
        payload: _DeliveryPayload,
        *,
        flush: bool,
        diagnostics: _RelayDiagnostics | None = None,
    ) -> int:
        """Record accepted values in the exact order they enter the FIFO."""
        dropped = original_enqueue(
            worker,
            stream,
            payload,
            flush=flush,
            diagnostics=diagnostics,
        )
        if dropped != 0:
            return dropped
        if not isinstance(payload.value, str):
            return dropped
        if not payload.value:
            return dropped
        queued_values.append(payload.value)
        return dropped

    monkeypatch.setattr(bounded_delivery._DeliveryWorker, "enqueue", record_enqueue)

    with scoped(ScopeConfig(allowlist=frozenset([program]))):
        result = _run_pipeline(
            pipeline,
            output=RunOutputOptions(
                capture=True,
                echo=True,
                delivery=BoundedDelivery(max_queued_bytes=4096),
            ),
            context=ExecutionContext(stdout_sink=sink, stderr_sink=sink),
        )

    assert result.stdout == "to-child\n"
    assert result.stages[0].stderr == "producer-error\n"
    assert sink.getvalue() == "".join(queued_values)
    assert len(queued_values) >= 2, "stdout and stderr must both enter the FIFO"


@pytest.mark.parametrize("failure_site", ["write", "flush"])
def test_pipeline_delivery_failures_keep_capture_and_stage_attribution(
    failure_site: str,
) -> None:
    """Pipeline write and flush failures retain capture and name their stage."""
    program, pipeline, _producer = _pipeline()
    sink = _FailingPipelineSink(failure_site)

    with scoped(ScopeConfig(allowlist=frozenset([program]))):
        result = _run_pipeline(
            pipeline,
            output=RunOutputOptions(
                capture=True,
                echo_stdout=False,
                echo_stderr=True,
                delivery=BoundedDelivery(max_queued_bytes=4096),
            ),
            context=ExecutionContext(stderr_sink=sink),
        )

    assert result.stdout == "to-child\n"
    assert result.stages[0].stderr == "producer-error\n"
    assert result.stages[0].relay_fallbacks == (
        RelayFallback(
            stream=EchoStream.STDERR,
            error_category=EchoErrorCategory.DELIVERY_ERROR,
        ),
    )
    assert result.stages[1].relay_fallbacks == ()
