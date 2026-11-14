"""Terminal hook dispatch completes adapter cleanup before surfacing errors."""

from __future__ import annotations

import asyncio
import dataclasses as dc

import pytest

from cuprum._observability import _emit_exec_event, _ExecEventEmissionError
from cuprum.adapters.tracing_adapter import TracingHook
from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.events import ExecEvent, TerminalOutcome, new_exec_id
from cuprum.unittests._cqrs_fixtures import _event


class _SyncObserveHookError(Exception):
    """Raised by the failing terminal hook in this test."""


def test_settled_dispatch_reaches_tracing_after_an_earlier_hook_fails() -> None:
    """All adapters receive settlement before its first hook error escapes."""
    tracer = InMemoryTracer()
    tracing_hook = TracingHook(tracer)
    exec_id = new_exec_id()

    def failing_hook(event: ExecEvent) -> None:
        """Fail only after the trace has been opened."""
        if event.phase == "settled":
            raise _SyncObserveHookError

    hooks = (failing_hook, tracing_hook)
    start_event = dc.replace(_event(), phase="start", exec_id=exec_id)
    settled_event = dc.replace(
        _event(),
        phase="settled",
        exec_id=exec_id,
        terminal_outcome=TerminalOutcome.ERROR,
    )
    _emit_exec_event(hooks, start_event)

    with pytest.raises(_ExecEventEmissionError) as exc_info:
        _emit_exec_event(hooks, settled_event)

    assert isinstance(exc_info.value.error, _SyncObserveHookError), (
        "settlement must preserve the first synchronous hook failure"
    )
    assert tracer.spans[0].ended, "the later tracing hook must close its span"
    assert exec_id not in tracing_hook._active_spans, (
        "the later tracing hook must remove the settled execution"
    )


def test_settled_failure_retains_tasks_scheduled_on_both_sides() -> None:
    """A settled failure keeps every async task, including those after it."""
    recorded: list[str] = []

    async def earlier(_event: ExecEvent) -> None:  # ruff: ignore[unused-async]
        """Record that the hook before the failure ran."""
        recorded.append("earlier")

    def failing(_event: ExecEvent) -> None:
        """Raise the terminal failure the dispatcher must surface."""
        raise _SyncObserveHookError

    async def later(_event: ExecEvent) -> None:  # ruff: ignore[unused-async]
        """Record that the hook after the failure still ran."""
        recorded.append("later")

    async def run() -> None:
        """Emit the terminal event and settle every retained task."""
        with pytest.raises(_ExecEventEmissionError) as exc_info:
            _emit_exec_event(
                (earlier, failing, later),
                dc.replace(_event(), phase="settled"),
            )
        emission = exc_info.value
        assert len(emission.scheduled_tasks) == 2, (
            "settlement must retain tasks scheduled after the failure too"
        )
        await asyncio.gather(*emission.scheduled_tasks, return_exceptions=True)

    asyncio.run(run())

    assert recorded == ["earlier", "later"], (
        "a settled failure must not stop later hooks"
    )
