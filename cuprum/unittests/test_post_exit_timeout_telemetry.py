"""Post-exit deadline expiry uses the normal timeout telemetry channels."""

from __future__ import annotations

import asyncio
import logging
import sys
import typing as typ

import pytest

from cuprum import sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions, StdinInput
from cuprum.unittests._post_exit_teardown_support import (
    PARKING_STDIN_BYTES,
    owned_builder,
    reap_descendant,
    recorded_descendant_or_fail,
)
from tests.helpers.timeouts import descendant_then_exit_argv, process_is_running

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.events import ExecEvent
    from cuprum.sh import SafeCmd

__all__ = ["owned_builder"]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX descendant cleanup"
)
_TIMEOUT_S = 1.0


async def _expire_after_exit(command: SafeCmd, route: str) -> None:
    """Reach a deadline with the leader reaped and a descendant retaining pipes."""
    context = ExecutionContext(process_group=ProcessGroupPolicy.INHERIT)
    if route == "direct":
        await command.run(
            timeout=_TIMEOUT_S,
            output=RunOutputOptions(capture=False),
            stdin=StdinInput(data=b"x" * PARKING_STDIN_BYTES),
            context=context,
        )
    else:
        async for _line in command.lines(timeout=_TIMEOUT_S, context=context):
            pass


@pytest.mark.parametrize("route", ["direct", "lines"])
def test_post_exit_timeout_reports_log_event_and_metric_once(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    route: str,
) -> None:
    """Each newly bounded route reports its configured deadline and child PID."""
    pid_file = tmp_path / f"{route}-descendant.pid"
    command = owned_builder(*descendant_then_exit_argv(pid_file, exit_code=0))
    events: list[ExecEvent] = []
    metrics = InMemoryMetrics()
    try:
        with (
            sh.observe(events.append),
            sh.observe(MetricsHook(metrics)),
            caplog.at_level(logging.WARNING, logger="cuprum.timeout"),
            pytest.raises(sh.TimeoutExpired) as caught,
        ):
            asyncio.run(_expire_after_exit(command, route))
        assert process_is_running(recorded_descendant_or_fail(pid_file)), (
            "an inherited expiry must leave its descendant alive before cleanup"
        )
        assert caught.value.timeout == _TIMEOUT_S
        started = [event for event in events if event.phase == "start"]
        timeouts = [event for event in events if event.phase == "timeout"]
        assert len(started) == len(timeouts) == 1, (
            f"one command must report one timeout, got {events!r}"
        )
        event = timeouts[0]
        assert event.pid == started[0].pid
        assert event.pid is not None
        assert (
            event.timeout_s,
            event.timeout_mode,
            event.operation,
            event.error_type,
        ) == (_TIMEOUT_S, "elapsed_deadline", "wait", "TimeoutError")
        records = [
            record for record in caplog.records if record.name == "cuprum.timeout"
        ]
        assert len(records) == 1, f"one expiry must log once, got {records!r}"
        fields = vars(records[0])
        assert (
            fields["cuprum_pid"],
            fields["cuprum_timeout_s"],
            fields["cuprum_timeout_mode"],
            fields["cuprum_error_type"],
        ) == (event.pid, _TIMEOUT_S, "elapsed_deadline", "TimeoutError")
        assert metrics.counters.get("cuprum_timeouts_total") == 1, (
            f"one expiry must increment the timeout counter once: {metrics.counters!r}"
        )
    finally:
        reap_descendant(pid_file, None)
