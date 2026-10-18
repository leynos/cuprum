"""Idle diagnostics share bounded delivery without blocking execution."""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest

from cuprum import ScopeConfig, TimeoutExpired, scoped
from cuprum.echo_events import (
    EchoErrorCategory,
    EchoEvent,
    EchoStream,
    RelayFallback,
)
from cuprum.echo_observation import observe_echo
from cuprum.sh import (
    BoundedDelivery,
    CommandResult,
    ExecutionContext,
    Pipeline,
    PipelineResult,
    RunOutputOptions,
    SafeCmd,
)
from cuprum.unittests._sink_test_support import BlockedSink
from tests.helpers.catalogue import PythonCatalogue, build_python_catalogue_env

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_IDLE_INTERVAL_S = 0.08


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


def _echoing_idle_case(
    run_kind: str,
) -> tuple[
    PythonCatalogue,
    SafeCmd | Pipeline,
    BlockedSink,
    RunOutputOptions,
    ExecutionContext,
]:
    """Build the blocked-sink case used to verify idle echo ordering."""
    runtime = build_python_catalogue_env()
    python = runtime.builder
    script = (
        "import time; print('first', flush=True); time.sleep(0.4); "
        "print('last', flush=True)"
    )
    target: SafeCmd | Pipeline = python("-c", script)
    if run_kind == "pipeline":
        target = _pipeline(python, script)
    sink = BlockedSink(wait_timeout_s=3.0)
    output = RunOutputOptions(
        capture=True,
        echo_stdout=True,
        echo_stderr=False,
        idle_after=_IDLE_INTERVAL_S,
        delivery=BoundedDelivery(max_queued_bytes=1024, drain_grace_s=1.0),
    )
    context = ExecutionContext(stdout_sink=sink, stderr_sink=sink)
    return runtime, target, sink, output, context


def _assert_echoing_idle_output(
    sink: BlockedSink,
    result: CommandResult | PipelineResult,
) -> None:
    """Check that keepalives are presented without entering capture."""
    lines = sink.getvalue().splitlines()
    heartbeat_index = next(
        index for index, line in enumerate(lines) if line.startswith("[cuprum]")
    )
    assert lines.index("first") < lines.index("last")
    assert heartbeat_index < lines.index("last")
    assert result.stdout == "first\nlast\n"
    assert "[cuprum]" not in result.stdout


@pytest.mark.parametrize("run_kind", ["command", "pipeline"])
def test_idle_output_uses_fifo_and_stays_out_of_capture(run_kind: str) -> None:
    """Keepalives use the shared FIFO and preserve captured child output."""
    runtime, target, sink, output, context = _echoing_idle_case(run_kind)

    async def run_and_release() -> CommandResult | PipelineResult:
        """Release the first blocked write after later output is queued."""
        task = asyncio.ensure_future(
            _target_call(target, output, context),
        )
        assert await asyncio.wait_for(
            asyncio.to_thread(sink.started.wait, 3.0),
            timeout=4.0,
        ), "child output must enter the delivery worker"
        await asyncio.sleep(0.7)
        sink.release.set()
        return await asyncio.wait_for(task, timeout=3.0)

    with scoped(ScopeConfig(allowlist=frozenset([runtime.program]))):
        try:
            result = asyncio.run(run_and_release())
        finally:
            sink.release.set()

    _assert_echoing_idle_output(sink, result)


def test_saturated_idle_delivery_reports_dropped_source_bytes() -> None:
    """A dropped keepalive is observable while command capture stays intact."""
    runtime = build_python_catalogue_env()
    command = runtime.builder("-c", "import time; time.sleep(0.4)")
    sink = io.StringIO()
    events: list[EchoEvent] = []

    with (
        observe_echo(events.append),
        scoped(
            ScopeConfig(allowlist=frozenset([runtime.program])),
        ),
    ):
        result = command.run_sync(
            output=RunOutputOptions(
                capture=True,
                echo=False,
                idle_after=_IDLE_INTERVAL_S,
                delivery=BoundedDelivery(max_queued_bytes=1),
            ),
            context=ExecutionContext(stderr_sink=sink),
        )

    expected_fallback = RelayFallback(
        stream=EchoStream.STDERR,
        error_category=EchoErrorCategory.DROPPED,
    )
    assert not result.stdout
    assert not result.stderr
    assert not sink.getvalue()
    assert expected_fallback in result.relay_fallbacks
    assert events
    assert all(
        event.stream is EchoStream.STDERR
        and event.error_category is EchoErrorCategory.DROPPED
        and event.dropped_bytes is not None
        and event.dropped_bytes > 0
        for event in events
    )


@pytest.mark.parametrize("run_kind", ["command", "pipeline"])
@pytest.mark.parametrize("stop_kind", ["timeout", "cancellation"])
def test_blocked_idle_delivery_does_not_delay_run_teardown(
    run_kind: str,
    stop_kind: str,
) -> None:
    """Timeout and cancellation settle while an idle keepalive is blocked."""
    runtime = build_python_catalogue_env()
    python = runtime.builder
    script = "import time; time.sleep(30)"
    command = python("-c", script)
    pipeline = _pipeline(python, script)
    sink = BlockedSink(wait_timeout_s=3.0)
    output = RunOutputOptions(
        capture=False,
        echo=False,
        idle_after=_IDLE_INTERVAL_S,
        delivery=BoundedDelivery(max_queued_bytes=1024, drain_grace_s=0.02),
    )
    context = ExecutionContext(
        stdout_sink=sink,
        stderr_sink=sink,
        cancel_grace=0.05,
    )

    async def start_and_stop() -> None:
        """Wait for a blocked heartbeat, then exercise its terminal path."""
        task = asyncio.ensure_future(
            _target_call(
                command if run_kind == "command" else pipeline,
                output,
                context,
                timeout=0.3 if stop_kind == "timeout" else None,
            ),
        )
        assert await asyncio.wait_for(
            asyncio.to_thread(sink.started.wait, 3.0),
            timeout=4.0,
        ), "idle output must reach the worker while the child is quiet"
        if stop_kind == "cancellation":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2.0)
        else:
            with pytest.raises(TimeoutExpired):
                await asyncio.wait_for(task, timeout=2.0)

    with scoped(ScopeConfig(allowlist=frozenset([runtime.program]))):
        try:
            asyncio.run(start_and_stop())
        finally:
            sink.release.set()
            if sink.started.is_set():
                assert sink.finished.wait(timeout=1.0)
