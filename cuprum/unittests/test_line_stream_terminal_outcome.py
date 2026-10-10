"""Terminal outcome classification for explicitly closed line streams."""

from __future__ import annotations

import asyncio

from cuprum import ScopeConfig, scoped, sh
from cuprum.events import ExecEvent, TerminalOutcome
from cuprum.sh import ExecutionContext
from tests.helpers.catalogue import python_catalogue


def test_aclose_settles_a_started_line_stream_as_cancelled() -> None:
    """Explicit stream teardown is cancellation, not an execution error."""
    catalogue, python_program = python_catalogue()
    command = sh.make(python_program, catalogue=catalogue)(
        "-c", "import time; print('ready', flush=True); time.sleep(30)"
    )
    events: list[ExecEvent] = []

    async def close_after_first_line() -> None:
        """Start the child, then close its stream while it is still running."""
        with (
            scoped(ScopeConfig(allowlist=catalogue.allowlist)),
            sh.observe(events.append),
        ):
            stream = command.lines(context=ExecutionContext(cancel_grace=0.1))
            try:
                first_line = await anext(stream)
                assert first_line.text == "ready", (
                    "explicit closure must happen after the child has started"
                )
            finally:
                await stream.aclose()

    asyncio.run(close_after_first_line())

    planned = next(event for event in events if event.phase == "plan")
    settled = next(event for event in events if event.phase == "settled")
    assert settled.terminal_outcome is TerminalOutcome.CANCELLED, (
        "closing the active stream must report cancellation"
    )
    assert settled.exec_id == planned.exec_id, (
        "the close outcome must correlate with the planned execution"
    )
