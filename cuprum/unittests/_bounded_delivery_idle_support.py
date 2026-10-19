"""Shared public-run helpers for bounded idle-delivery tests."""

from __future__ import annotations

import typing as typ

from cuprum._bounded_delivery import _DeliveryWorker

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum._bounded_delivery import _DeliveryPayload
    from cuprum._stream_drain_state import _RelayDiagnostics
    from cuprum.echo_events import EchoStream
    from cuprum.sh import (
        CommandResult,
        ExecutionContext,
        Pipeline,
        PipelineResult,
        RunOutputOptions,
        SafeCmd,
    )


def _pipeline(
    python: cabc.Callable[..., SafeCmd],
    script: str,
) -> Pipeline:
    """Build a pipeline whose final stage forwards producer output."""
    return python("-c", script) | python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )


def _target_call(
    target: SafeCmd | Pipeline,
    output: RunOutputOptions,
    context: ExecutionContext,
    *,
    timeout: float | None = None,
) -> cabc.Awaitable[CommandResult | PipelineResult]:
    """Call the selected public command or pipeline route."""
    return target.run(output=output, context=context, timeout=timeout)


async def _run_target(
    target: SafeCmd | Pipeline,
    output: RunOutputOptions,
    context: ExecutionContext,
) -> CommandResult | PipelineResult:
    """Await either selected route from an ``asyncio.run`` coroutine."""
    return await _target_call(target, output, context)


def _record_accepted_idle_bytes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Observe accepted idle payload sizes without changing worker results."""
    accepted_bytes: list[int] = []
    original_enqueue = _DeliveryWorker.enqueue

    def record_enqueue(
        worker: _DeliveryWorker,
        stream: EchoStream,
        payload: _DeliveryPayload,
        *,
        flush: bool,
        diagnostics: _RelayDiagnostics | None = None,
    ) -> int:
        """Record source bytes for idle payloads accepted into the queue."""
        rejected_bytes = original_enqueue(
            worker,
            stream,
            payload,
            flush=flush,
            diagnostics=diagnostics,
        )
        if diagnostics is not None and rejected_bytes == 0:
            accepted_bytes.append(payload.source_bytes)
        return rejected_bytes

    monkeypatch.setattr(_DeliveryWorker, "enqueue", record_enqueue)
    return accepted_bytes
