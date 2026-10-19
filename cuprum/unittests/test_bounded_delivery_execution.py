"""Timeout, cancellation, and child reaping with a blocked output writer."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import subprocess  # ruff: ignore[suspicious-subprocess-import] - this test isolates interpreter shutdown.
import sys
import textwrap
import typing as typ

import pytest

import cuprum._bounded_delivery as bounded_delivery
import cuprum._command_execution_config as command_execution_config
import cuprum._pipeline_config as pipeline_config
from cuprum import ScopeConfig, TimeoutExpired, scoped, sh
from cuprum.echo_events import EchoErrorCategory, EchoStream
from cuprum.echo_observation import observe_echo
from cuprum.events import TerminalOutcome
from cuprum.sh import (
    BoundedDelivery,
    ExecutionContext,
    RunOutputOptions,
)
from cuprum.unittests._sink_test_support import BlockedSink, RecordingSink
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import started_pids, wait_for_process_death

if typ.TYPE_CHECKING:
    from cuprum._bounded_delivery import _DeliveryWorker
    from cuprum.echo_events import EchoEvent
    from cuprum.events import ExecEvent
    from cuprum.program import Program
    from cuprum.sh import Pipeline, SafeCmd

_BLOCKED_OUTPUT = "blocked-delivery"
_COMMAND_SCRIPT = "import time; print('blocked-delivery', flush=True); time.sleep(30)"
_PIPELINE_WRITER_SCRIPT = (
    "import time; print('blocked-delivery', flush=True); time.sleep(30)"
)
_PIPELINE_READER_SCRIPT = (
    "import sys, time; line = sys.stdin.readline(); "
    "sys.stdout.write(line); sys.stdout.flush(); time.sleep(30)"
)


@dc.dataclass(slots=True)
class _BlockedDeliveryScenario:
    """Shared state for one blocked-writer execution scenario."""

    subject: SafeCmd | Pipeline
    expected_children: int
    program: Program
    sink: BlockedSink
    execution_events: list[ExecEvent] = dc.field(default_factory=list)
    echo_events: list[EchoEvent] = dc.field(default_factory=list)
    timeout_error: TimeoutExpired | None = None


def _build_subject(kind: str) -> _BlockedDeliveryScenario:
    """Build a command or two-stage pipeline that stays live after writing."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    if kind == "command":
        subject = python("-c", _COMMAND_SCRIPT)
        expected_children = 1
    else:
        producer = python("-c", _PIPELINE_WRITER_SCRIPT)
        consumer = python("-c", _PIPELINE_READER_SCRIPT)
        subject = producer | consumer
        expected_children = 2
    return _BlockedDeliveryScenario(
        subject=subject,
        expected_children=expected_children,
        program=python_program,
        sink=BlockedSink(),
    )


async def _run_subject(
    subject: SafeCmd | Pipeline,
    *,
    deadline_s: float | None,
    sink: BlockedSink,
) -> object:
    """Run the selected subject with capture and bounded presentation."""
    return await subject.run(
        timeout=deadline_s,
        output=RunOutputOptions(
            capture=True,
            echo_stdout=True,
            echo_stderr=False,
            delivery=BoundedDelivery(max_queued_bytes=128, drain_grace_s=0.02),
        ),
        context=ExecutionContext(stdout_sink=sink, cancel_grace=0.1),
    )


async def _exercise_blocked_delivery(
    scenario: _BlockedDeliveryScenario,
    ending: str,
) -> None:
    """Wait for the blocked write, then finish and release its run."""
    task = asyncio.create_task(
        _run_subject(
            scenario.subject,
            deadline_s=1.0 if ending == "timeout" else None,
            sink=scenario.sink,
        ),
    )
    try:
        writer_started = await asyncio.wait_for(
            asyncio.to_thread(scenario.sink.started.wait, 4.0),
            timeout=5.0,
        )
        assert writer_started, "the delivery worker must enter sink.write"
        assert scenario.sink.writer_thread_name == "cuprum-output-delivery", (
            "sink.write must run on the dedicated delivery worker"
        )
        if ending == "timeout":
            with pytest.raises(TimeoutExpired) as exc_info:
                await task
            scenario.timeout_error = exc_info.value
        else:
            assert task.cancel(), "the running command must accept cancellation"
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not scenario.sink.finished.is_set(), (
            "run finalization must not wait for the blocked writer"
        )
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        scenario.sink.release.set()
        writer_finished = await asyncio.wait_for(
            asyncio.to_thread(scenario.sink.finished.wait, 2.0),
            timeout=3.0,
        )
        assert writer_finished, "the test must release its blocked writer"


@pytest.mark.parametrize("kind", ["command", "pipeline"])
@pytest.mark.parametrize("ending", ["timeout", "cancellation"])
def test_blocked_delivery_does_not_delay_timeout_or_cancellation(
    kind: str,
    ending: str,
) -> None:
    """The loop settles and reaps children while its presentation worker blocks."""
    scenario = _build_subject(kind)

    with (
        scoped(ScopeConfig(allowlist=frozenset([scenario.program]))),
        sh.observe(scenario.execution_events.append),
        observe_echo(scenario.echo_events.append),
    ):
        asyncio.run(_exercise_blocked_delivery(scenario, ending))

    pids = started_pids(scenario.execution_events)
    assert len(pids) == scenario.expected_children, (
        f"{kind} must start {scenario.expected_children} child process(es), "
        f"got {pids!r}"
    )
    for pid in pids:
        wait_for_process_death(pid, seconds=3.0, context=f"{kind} {ending}")

    expected_outcome = (
        TerminalOutcome.TIMEOUT if ending == "timeout" else TerminalOutcome.CANCELLED
    )
    settled = [event for event in scenario.execution_events if event.phase == "settled"]
    assert len(settled) == scenario.expected_children
    assert all(event.terminal_outcome is expected_outcome for event in settled)

    abandoned = [
        event
        for event in scenario.echo_events
        if event.error_category is EchoErrorCategory.UNDELIVERED
    ]
    assert len(abandoned) == 1
    assert abandoned[0].dropped_bytes is not None
    assert abandoned[0].dropped_bytes > 0
    if scenario.timeout_error is not None:
        assert isinstance(scenario.timeout_error.stdout, str)
        assert _BLOCKED_OUTPUT in scenario.timeout_error.stdout


def test_failed_execution_bundle_build_closes_delivery_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preparation failure cannot strand the newly started daemon worker."""
    scenario = _build_subject("command")
    created_workers: list[_DeliveryWorker] = []
    original_worker = bounded_delivery._DeliveryWorker
    run_error = RuntimeError("idle monitor setup failed")

    def record_worker_creation(
        destinations: dict[EchoStream, typ.IO[str] | typ.BinaryIO],
        max_queued_bytes: int,
    ) -> _DeliveryWorker:
        """Record worker ownership while preserving its real thread lifecycle."""
        worker = original_worker(destinations, max_queued_bytes)
        created_workers.append(worker)
        return worker

    def fail_idle_monitor(*_args: object, **_kwargs: object) -> None:
        """Fail after the delivery worker starts but before execution returns."""
        raise run_error

    monkeypatch.setattr(
        bounded_delivery,
        "_DeliveryWorker",
        record_worker_creation,
    )
    monkeypatch.setattr(
        command_execution_config,
        "_build_idle_monitor",
        fail_idle_monitor,
    )

    with (
        scoped(ScopeConfig(allowlist=frozenset([scenario.program]))),
        pytest.raises(RuntimeError) as exc_info,
    ):
        asyncio.run(
            _run_subject(
                scenario.subject,
                deadline_s=None,
                sink=scenario.sink,
            ),
        )

    assert exc_info.value is run_error
    assert len(created_workers) == 1
    assert created_workers[0]._closing
    created_workers[0]._thread.join(timeout=1.0)
    assert not created_workers[0]._thread.is_alive()
    assert not scenario.sink.started.is_set()


def test_failed_pipeline_config_build_closes_delivery_worker_and_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline preparation failure releases its worker and sink session."""
    sink = RecordingSink()
    created_workers: list[_DeliveryWorker] = []
    original_worker = bounded_delivery._DeliveryWorker
    run_error = RuntimeError("idle monitor setup failed")

    def record_worker_creation(
        destinations: dict[EchoStream, typ.IO[str] | typ.BinaryIO],
        max_queued_bytes: int,
    ) -> _DeliveryWorker:
        """Record the pipeline worker while retaining its real lifecycle."""
        worker = original_worker(destinations, max_queued_bytes)
        created_workers.append(worker)
        return worker

    def fail_idle_monitor(*_args: object, **_kwargs: object) -> None:
        """Fail after the sink and delivery worker have been acquired."""
        raise run_error

    monkeypatch.setattr(bounded_delivery, "_DeliveryWorker", record_worker_creation)
    monkeypatch.setattr(pipeline_config, "_build_idle_monitor", fail_idle_monitor)

    with pytest.raises(RuntimeError) as exc_info:
        pipeline_config._prepare_pipeline_config(
            output=RunOutputOptions(
                sink=sink,
                delivery=BoundedDelivery(max_queued_bytes=128),
            ),
            timeout=None,
            context=None,
        )

    assert exc_info.value is run_error
    assert len(created_workers) == 1
    assert created_workers[0]._closing
    created_workers[0]._thread.join(timeout=1.0)
    assert not created_workers[0]._thread.is_alive()
    assert sink.last_session.closed_event.wait(timeout=1.0)
    assert sink.last_session.closed == 1
    assert sink.recorded_outcome.outcome is TerminalOutcome.ERROR


def test_blocked_delivery_worker_does_not_join_at_interpreter_exit() -> None:
    """A blocked delivery daemon cannot hold interpreter shutdown open."""
    script = textwrap.dedent(
        """
        from cuprum._bounded_delivery import _DeliveryPayload, _DeliveryWorker
        from cuprum.echo_events import EchoStream
        from cuprum.unittests._sink_test_support import BlockedSink

        sink = BlockedSink()
        worker = _DeliveryWorker(
            destinations={EchoStream.STDOUT: sink, EchoStream.STDERR: sink},
            max_queued_bytes=64,
        )
        worker.enqueue(
            EchoStream.STDOUT,
            _DeliveryPayload(value="blocked", source_bytes=7),
            flush=True,
        )
        if not sink.started.wait(timeout=1):
            raise RuntimeError("delivery worker did not start")
        result = worker.close(grace=0.01)
        if not result.undelivered_bytes:
            raise RuntimeError("close must report the blocked write")
        if not worker._thread.daemon or not worker._thread.is_alive():
            raise RuntimeError("the blocked writer must remain an abandoned daemon")
        """
    )
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed interpreter invocation.
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
        timeout=5.0,
    )

    assert completed.returncode == 0, completed.stderr
