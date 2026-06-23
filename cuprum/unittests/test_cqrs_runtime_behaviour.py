"""Runtime coverage for CQRS helper refactoring behaviour.

This module exercises the command-query separation helper changes through
their runtime call paths rather than only through isolated unit helpers.  It
covers allowlist enforcement before hook collection, observe-hook task
scheduling and failure ordering, shared pending-task state across pipeline
stage observations, and pipeline stream coordination after an early downstream
close.

The tests deliberately mix public execution paths with small local stubs.
`SafeCmd.run_sync()` and `Pipeline.run_sync()` cover end-to-end behaviour,
while stub stream readers/writers let the pump invariants be generated without
spawning hundreds of subprocesses.  Hypothesis varies hook orderings, stream
payload chunks, and downstream failure points so regressions surface as small
counterexamples instead of single hand-picked scenarios.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum import (
    ECHO,
    LS,
    ForbiddenProgramError,
    ScopeConfig,
    before,
    scoped,
    sh,
)
from cuprum._observability import (
    _emit_exec_event,
    _ExecEventEmissionError,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_types import _EventDetails, _ExecutionHooks, _StageObservation
from cuprum._streams import _pump_stream
from cuprum.events import ExecEvent
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _GeneratedHookError(Exception):
    """Test exception raised by generated hook-ordering cases."""


class _StubPumpReader:
    """Stub stream reader yielding queued chunks then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Initialise the stub with queued chunks."""
        self._chunks = list(chunks)
        self.read_calls = 0

    async def read(self, _: int) -> bytes:
        """Return the next queued chunk, or empty bytes at EOF."""
        self.read_calls += 1
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _NeverEndingPumpReader:
    """Stub stream reader that emits once and never reaches EOF."""

    def __init__(self) -> None:
        """Initialise the reader."""
        self.read_calls = 0

    async def read(self, _: int) -> bytes:
        """Return one chunk, then wait forever unless the pump cancels the read."""
        self.read_calls += 1
        if self.read_calls == 1:
            return b"first"
        await asyncio.sleep(3600)
        return b"more"


class _StubPumpWriter:
    """Stub stream writer recording writes, drains, and closure."""

    def __init__(self, *, fail_on_drain_call: int | None = None) -> None:
        """Initialise the stub with an optional drain failure point."""
        self.drain_calls = 0
        self.closed = False
        self.wait_closed_calls = 0
        self._fail_on_drain_call = fail_on_drain_call

    def write(self, _chunk: bytes) -> None:
        """Accept a chunk like ``asyncio.StreamWriter.write``."""

    async def drain(self) -> None:
        """Record drain and optionally simulate downstream closure."""
        self.drain_calls += 1
        await asyncio.sleep(0)
        if self.drain_calls == self._fail_on_drain_call:
            raise BrokenPipeError

    def write_eof(self) -> None:
        """Accept EOF signalling like ``asyncio.StreamWriter.write_eof``."""

    def close(self) -> None:
        """Mark the writer as closed."""
        self.closed = True

    async def wait_closed(self) -> None:
        """Record that closure was awaited."""
        self.wait_closed_calls += 1


def _echo_cmd() -> sh.SafeCmd:
    """Return a trivial ``SafeCmd`` for the allowlisted ``echo`` program."""
    return sh.make(ECHO)("hello")


def _event() -> ExecEvent:
    """Return a minimal ``plan`` event for hook dispatch."""
    return ExecEvent(
        phase="plan",
        program=ECHO,
        argv=(str(ECHO),),
        cwd=None,
        env=None,
        pid=None,
        timestamp=0.0,
        line=None,
        exit_code=None,
        duration_s=None,
        tags={},
    )


def test_safe_cmd_run_enforces_allowlist_before_before_hooks() -> None:
    """A forbidden ``SafeCmd.run_sync`` must not dispatch before hooks."""
    calls: list[sh.SafeCmd] = []

    def track_before(cmd: sh.SafeCmd) -> None:
        """Record whether a before hook ran."""
        calls.append(cmd)

    with (
        scoped(ScopeConfig(allowlist=frozenset([LS]))),
        before(track_before),
        pytest.raises(ForbiddenProgramError),
    ):
        _echo_cmd().run_sync()

    assert calls == [], "before hooks should not run before allowlist enforcement"


@given(
    hook_kinds=st.lists(
        st.sampled_from(("sync", "async", "fail")),
        min_size=1,
        max_size=8,
    )
)
def test_emit_exec_event_preserves_scheduled_prefix_for_generated_hook_orders(
    hook_kinds: list[str],
) -> None:
    """Generated hook orderings preserve tasks scheduled before failure."""

    async def run() -> None:
        """Drive generated hooks inside a running loop."""
        hooks = tuple(_generated_observe_hook(kind) for kind in hook_kinds)
        first_failure_index = _first_failure_index(hook_kinds)
        expected_scheduled = hook_kinds[
            : first_failure_index if first_failure_index is not None else None
        ].count("async")

        if first_failure_index is None:
            scheduled = _emit_exec_event(hooks, _event())
            assert len(scheduled) == expected_scheduled, (
                "all generated async hooks should be scheduled when no hook fails"
            )
            await asyncio.gather(*scheduled)
            return

        with pytest.raises(_ExecEventEmissionError) as exc_info:
            _emit_exec_event(hooks, _event())
        assert len(exc_info.value.scheduled_tasks) == expected_scheduled, (
            "failing hook emission should carry tasks scheduled before failure"
        )
        await asyncio.gather(*exc_info.value.scheduled_tasks)

    asyncio.run(run())


def _first_failure_index(hook_kinds: list[str]) -> int | None:
    """Return the index of the first generated failing hook."""
    try:
        return hook_kinds.index("fail")
    except ValueError:
        return None


def _generated_observe_hook(
    kind: str,
) -> cabc.Callable[[ExecEvent], cabc.Awaitable[None] | None]:
    """Build a generated observe hook of the requested kind."""

    async def async_hook(_event: ExecEvent) -> None:
        """Cross an async boundary for generated hook scheduling."""
        await asyncio.sleep(0)

    def sync_hook(event: ExecEvent) -> None:
        """Run a generated synchronous hook."""
        _ = event.phase

    def fail_hook(_event: ExecEvent) -> None:
        """Raise from a generated hook."""
        raise _GeneratedHookError

    match kind:
        case "async":
            return async_hook
        case "sync":
            return sync_hook
        case "fail":
            return fail_hook
        case _:
            msg = "unknown generated hook kind"
            raise ValueError(msg)


def test_stage_observations_share_pending_tasks_under_concurrent_emits() -> None:
    """Concurrent stage emissions append to the shared pending-task list."""

    async def run() -> None:
        """Emit from multiple stage observations in one event loop."""
        pending_tasks: list[asyncio.Task[None]] = []
        completed: list[int] = []
        observations = _stage_observations(pending_tasks, completed)

        await asyncio.gather(
            *itertools.starmap(_emit_after_yield, enumerate(observations))
        )

        assert len(pending_tasks) == len(observations), (
            "each concurrent stage emission should append one pending task"
        )
        await _wait_for_exec_hook_tasks(pending_tasks)
        assert sorted(completed) == list(range(len(observations))), (
            "all scheduled stage observe tasks should complete exactly once"
        )

    asyncio.run(run())


def _stage_observations(
    pending_tasks: list[asyncio.Task[None]],
    completed: list[int],
) -> tuple[_StageObservation, ...]:
    """Build observations sharing one pending-task collection."""

    async def async_hook(event: ExecEvent) -> None:
        """Record the stage index after an async scheduling boundary."""
        await asyncio.sleep(0)
        completed.append(typ.cast("int", event.tags["pipeline_stage_index"]))

    return tuple(
        _StageObservation(
            cmd=_echo_cmd(),
            hooks=_ExecutionHooks(
                before_hooks=(),
                after_hooks=(),
                observe_hooks=(async_hook,),
            ),
            tags={"pipeline_stage_index": idx},
            cwd=None,
            env_overlay=None,
            pending_tasks=pending_tasks,
        )
        for idx in range(4)
    )


async def _emit_after_yield(idx: int, observation: _StageObservation) -> None:
    """Yield once, then emit on the active event loop."""
    await asyncio.sleep(0)
    observation.emit("start", _EventDetails(pid=idx))


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_drains_upstream_after_downstream_closes() -> None:
    """Pipeline subprocess I/O completes when a downstream stage exits early."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python(
        "-c",
        "\n".join(
            (
                "import sys",
                "for _ in range(4096):",
                "    sys.stdout.write('payload-line\\n')",
                "    sys.stdout.flush()",
            ),
        ),
    )
    early_consumer = python(
        "-c",
        "import sys; sys.stdin.buffer.read(1); sys.stdout.write('done')",
    )

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = (producer | early_consumer).run_sync(timeout=5.0)

    assert result.stdout == "done", "final stage should report completion"
    assert result.stages[1].exit_code == 0, "early consumer should exit cleanly"


def test_pump_stream_logs_discarded_bytes_after_downstream_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Early downstream closure logs how many upstream bytes were discarded."""

    async def exercise() -> None:
        """Pump through a writer that fails mid-stream."""
        reader = _StubPumpReader([b"a" * 8, b"b" * 8, b"c"])
        writer = _StubPumpWriter(fail_on_drain_call=2)
        await _pump_stream(
            typ.cast("asyncio.StreamReader", reader),
            typ.cast("asyncio.StreamWriter", writer),
        )

    with caplog.at_level(logging.DEBUG, logger="cuprum.streams"):
        asyncio.run(exercise())

    discarded_records = [
        record
        for record in caplog.records
        if record.name == "cuprum.streams"
        and "stream_downstream_closed" in record.message
    ]
    assert discarded_records, "early downstream close should log discarded bytes"
    last_discard = discarded_records[-1]
    assert vars(last_discard)["cuprum_discarded_bytes"] == 0, (
        "early-close telemetry should happen before best-effort draining"
    )


def test_pump_stream_omits_downstream_close_log_without_writer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing writers drain quietly without reporting downstream closure."""

    async def exercise() -> _StubPumpReader:
        """Pump with no writer so the reader is simply drained."""
        reader = _StubPumpReader([b"a" * 8, b"b" * 8])
        await _pump_stream(typ.cast("asyncio.StreamReader", reader), None)
        return reader

    with caplog.at_level(logging.DEBUG, logger="cuprum.streams"):
        reader = asyncio.run(exercise())

    close_records = [
        record
        for record in caplog.records
        if record.name == "cuprum.streams"
        and "stream_downstream_closed" in record.message
    ]
    assert reader.read_calls == 3, "pump should drain all chunks plus EOF"
    assert not close_records, "missing writer should not log downstream closure"


def test_pump_stream_does_not_hang_when_upstream_never_ends() -> None:
    """Early downstream closure performs bounded draining only."""

    async def exercise() -> _NeverEndingPumpReader:
        """Pump from a reader that never produces EOF."""
        reader = _NeverEndingPumpReader()
        writer = _StubPumpWriter(fail_on_drain_call=1)
        await _pump_stream(
            typ.cast("asyncio.StreamReader", reader),
            typ.cast("asyncio.StreamWriter", writer),
        )
        return reader

    reader = asyncio.run(asyncio.wait_for(exercise(), timeout=1.0))

    assert reader.read_calls == 2, "pump should cancel the bounded drain read"


@given(
    chunks=st.lists(st.binary(min_size=1, max_size=64), min_size=0, max_size=8),
    fail_on_drain_call=st.integers(min_value=1, max_value=10),
)
def test_pump_stream_drains_generated_chunks_after_downstream_close(
    chunks: list[bytes],
    fail_on_drain_call: int,
) -> None:
    """Generated chunk streams are drained to EOF after downstream closure."""

    async def exercise() -> tuple[_StubPumpReader, _StubPumpWriter]:
        """Pump generated chunks through a writer with generated failure point."""
        reader = _StubPumpReader(chunks)
        writer = _StubPumpWriter(fail_on_drain_call=fail_on_drain_call)
        await _pump_stream(
            typ.cast("asyncio.StreamReader", reader),
            typ.cast("asyncio.StreamWriter", writer),
        )
        return reader, writer

    reader, writer = asyncio.run(exercise())

    assert reader.read_calls == len(chunks) + 1, (
        "pump should always read every generated chunk plus EOF"
    )
    assert writer.closed is True, "pump should close the writer after draining"
    assert writer.wait_closed_calls == 1, "pump should await writer closure once"
