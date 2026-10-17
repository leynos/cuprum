"""Delivery-worker ownership for ``SafeCmd.lines()``."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

import cuprum._command_execution_config as command_execution_config
from cuprum import ScopeConfig, scoped, sh
from cuprum.echo_events import EchoErrorCategory, EchoStream
from cuprum.echo_observation import observe_echo
from cuprum.sh import BoundedDelivery, ExecutionContext, RunOutputOptions
from cuprum.unittests._sink_test_support import BlockedSink
from tests.helpers.catalogue import build_python_catalogue_env
from tests.helpers.timeouts import started_pids, wait_for_process_death

if typ.TYPE_CHECKING:
    from cuprum._bounded_delivery import _DeliveryWorker
    from cuprum.echo_events import EchoEvent
    from cuprum.events import ExecEvent
    from cuprum.sh import CommandResult, SafeCmd


def test_lines_defers_delivery_setup_until_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing an unstarted line stream creates no delivery thread."""
    runtime = build_python_catalogue_env()
    command = runtime.builder("-c", "print('line')")
    sink = BlockedSink()
    created_workers: list[bool] = []
    original_worker = command_execution_config._DeliveryWorker

    def record_worker_creation(
        destinations: dict[EchoStream, typ.IO[str] | typ.BinaryIO],
        max_queued_bytes: int,
    ) -> _DeliveryWorker:
        """Record worker ownership while preserving its actual behaviour."""
        created_workers.append(True)
        return original_worker(destinations, max_queued_bytes)

    monkeypatch.setattr(
        command_execution_config,
        "_DeliveryWorker",
        record_worker_creation,
    )
    with scoped(ScopeConfig(allowlist=frozenset([runtime.program]))):
        stream = command.lines(
            output=RunOutputOptions(
                echo_stdout=True,
                echo_stderr=False,
                delivery=BoundedDelivery(max_queued_bytes=128),
            ),
            context=ExecutionContext(stdout_sink=sink),
        )
        assert not created_workers
        asyncio.run(stream.aclose())

    assert not created_workers
    assert not sink.started.is_set()


async def _consume_blocked_line_stream(
    command: SafeCmd,
    output: RunOutputOptions,
    sink: BlockedSink,
    *,
    close_early: bool,
) -> CommandResult | None:
    """Block delivery, then complete or explicitly close the line iterator."""
    stream = command.lines(
        output=output,
        context=ExecutionContext(stdout_sink=sink, cancel_grace=0.1),
    )
    try:
        line = await anext(stream)
        assert line.text == "ready"
        writer_started = await asyncio.wait_for(
            asyncio.to_thread(sink.started.wait, 3.0),
            timeout=4.0,
        )
        assert writer_started
        if close_early:
            await stream.aclose()
        else:
            async for _line in stream:
                pass
        assert not sink.finished.is_set()
        return stream.result
    finally:
        await stream.aclose()
        sink.release.set()
        writer_finished = await asyncio.wait_for(
            asyncio.to_thread(sink.finished.wait, 2.0),
            timeout=3.0,
        )
        assert writer_finished


@pytest.mark.parametrize("close_early", [False, True])
def test_lines_closes_blocked_delivery_worker(
    close_early: bool,
) -> None:
    """Normal completion and early close settle delivery before teardown."""
    runtime = build_python_catalogue_env()
    script = (
        "import time; print('ready', flush=True); time.sleep(30)"
        if close_early
        else "print('ready', flush=True)"
    )
    command = runtime.builder("-c", script)
    sink = BlockedSink()
    execution_events: list[ExecEvent] = []
    echo_events: list[EchoEvent] = []
    output = RunOutputOptions(
        capture=True,
        echo_stdout=True,
        echo_stderr=False,
        delivery=BoundedDelivery(max_queued_bytes=128, drain_grace_s=0.02),
    )

    with (
        scoped(ScopeConfig(allowlist=frozenset([runtime.program]))),
        sh.observe(execution_events.append),
        observe_echo(echo_events.append),
    ):
        result = asyncio.run(
            _consume_blocked_line_stream(
                command,
                output,
                sink,
                close_early=close_early,
            ),
        )

    pids = started_pids(execution_events)
    assert len(pids) == 1
    for pid in pids:
        wait_for_process_death(pid, seconds=3.0, context="line stream teardown")

    undelivered = [
        event
        for event in echo_events
        if event.error_category is EchoErrorCategory.UNDELIVERED
    ]
    assert len(undelivered) == 1
    assert undelivered[0].stream is EchoStream.STDOUT
    assert undelivered[0].dropped_bytes == len(b"ready\n")
    if close_early:
        assert result is None
    else:
        assert result is not None
        assert result.stdout == "ready\n"
        assert any(
            fallback.error_category is EchoErrorCategory.UNDELIVERED
            for fallback in result.relay_fallbacks
        )
