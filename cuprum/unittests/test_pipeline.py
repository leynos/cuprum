"""Unit tests for Pipeline composition and execution."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ECHO, scoped, sh
from cuprum.sh import (
    _READ_SIZE,
    Pipeline,
    PipelineResult,
    _prepare_pipeline_config,
    _pump_stream,
    _spawn_pipeline_processes,
)
from tests.helpers.catalogue import python_catalogue


class _StubPumpReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.read_calls = 0

    async def read(self, _: int) -> bytes:
        self.read_calls += 1
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _StubPumpWriter:
    def __init__(self, *, fail_on_drain_call: int | None = None) -> None:
        self.data = bytearray()
        self.drain_calls = 0
        self.write_calls = 0
        self.closed = False
        self.write_eof_calls = 0
        self.wait_closed_calls = 0
        self._fail_on_drain_call = fail_on_drain_call

    def write(self, chunk: bytes) -> None:
        self.write_calls += 1
        self.data.extend(chunk)

    async def drain(self) -> None:
        self.drain_calls += 1
        await asyncio.sleep(0)
        if self._fail_on_drain_call is None:
            return
        if self.drain_calls == self._fail_on_drain_call:
            raise BrokenPipeError

    def write_eof(self) -> None:
        self.write_eof_calls += 1

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1


class _StubSpawnProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = None
        self.stderr = None
        self.stdin = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        await asyncio.sleep(0)
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


def test_or_operator_composes_pipeline() -> None:
    """The | operator composes SafeCmd stages into a Pipeline."""
    echo = sh.make(ECHO)
    first = echo("-n", "hello")
    second = echo("-n", "world")

    pipeline = first | second

    assert isinstance(pipeline, Pipeline)
    assert pipeline.parts == (first, second)


def test_or_operator_composes_safe_cmd_with_pipeline() -> None:
    """SafeCmd | Pipeline prepends the command to the pipeline."""
    echo = sh.make(ECHO)
    first = echo("-n", "hello")
    second = echo("-n", "world")
    third = echo("-n", "!")

    pipeline = first | (second | third)

    assert pipeline.parts == (first, second, third)


def test_or_operator_composes_pipeline_with_pipeline() -> None:
    """Pipeline | Pipeline concatenates stages in order."""
    echo = sh.make(ECHO)
    first = echo("-n", "one")
    second = echo("-n", "two")
    third = echo("-n", "three")
    fourth = echo("-n", "four")

    left_pipeline = first | second
    right_pipeline = third | fourth
    pipeline = left_pipeline | right_pipeline

    assert pipeline.parts == (first, second, third, fourth)


def test_pipeline_can_append_stages() -> None:
    """Pipeline | SafeCmd extends the pipeline in order."""
    echo = sh.make(ECHO)
    first = echo("-n", "one")
    second = echo("-n", "two")
    third = echo("-n", "three")

    pipeline = first | second | third

    assert pipeline.parts == (first, second, third)


def test_pipeline_run_streams_stdout_between_stages() -> None:
    """Pipeline.run_sync streams stdout into the next stage stdin."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    echo = sh.make(ECHO)

    pipeline = echo("-n", "hello") | python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read().upper())",
    )

    with scoped(allowlist=frozenset([ECHO, python_program])):
        result = pipeline.run_sync()

    assert isinstance(result, PipelineResult)
    assert result.stdout == "HELLO"
    assert len(result.stages) == 2
    assert result.stages[0].stdout is None
    assert result.stages[0].exit_code == 0
    assert result.stages[1].exit_code == 0
    assert result.stages[0].pid > 0
    assert result.stages[1].pid > 0


def test_pipeline_run_sync_failure_sets_ok_false_and_final_to_failed_stage() -> None:
    """Pipeline.run_sync reports failure when a stage exits non-zero."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    pipeline = python("-c", "import sys; sys.exit(0)") | python(
        "-c",
        "import sys; sys.exit(1)",
    )

    with scoped(allowlist=frozenset([python_program])):
        result = pipeline.run_sync()

    assert isinstance(result, PipelineResult)
    assert result.ok is False
    assert result.final is result.stages[-1]
    assert result.final.exit_code == 1
    assert len(result.stages) == 2
    assert result.stages[0].exit_code == 0
    assert result.stages[1].exit_code == 1


def test_pump_stream_drains_per_chunk() -> None:
    """Streaming between stages awaits drain for backpressure."""

    async def exercise() -> _StubPumpWriter:
        reader = asyncio.StreamReader()
        writer = _StubPumpWriter()
        task = asyncio.create_task(
            _pump_stream(reader, typ.cast("asyncio.StreamWriter", writer)),
        )
        payload = b"x" * (_READ_SIZE * 2 + 1)
        reader.feed_data(payload)
        reader.feed_eof()
        await task
        return writer

    writer = asyncio.run(exercise())

    assert bytes(writer.data) == b"x" * (_READ_SIZE * 2 + 1)
    assert writer.write_calls == writer.drain_calls
    assert writer.drain_calls >= 2
    assert writer.write_eof_calls == 1
    assert writer.closed is True
    assert writer.wait_closed_calls == 1


def test_pump_stream_handles_downstream_close_without_hanging() -> None:
    """_pump_stream drains stdout even if downstream closes mid-stream."""

    async def exercise() -> tuple[_StubPumpReader, _StubPumpWriter]:
        reader = _StubPumpReader([b"a" * _READ_SIZE, b"b" * _READ_SIZE, b"c"])
        writer = _StubPumpWriter(fail_on_drain_call=2)
        await _pump_stream(
            typ.cast("asyncio.StreamReader", reader),
            typ.cast("asyncio.StreamWriter", writer),
        )
        return reader, writer

    reader, writer = asyncio.run(exercise())

    assert reader.read_calls == 4  # 3 chunks + EOF
    assert writer.write_calls == 2  # third chunk is drained but not written
    assert writer.closed is True
    assert writer.wait_closed_calls == 1


def test_spawn_pipeline_processes_terminates_started_stages_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawn failures should terminate any already-started pipeline stages."""
    echo = sh.make(ECHO)
    first = echo("-n", "hello")
    second = echo("-n", "world")
    config = _prepare_pipeline_config(capture=True, echo=False, context=None)

    spawned: list[_StubSpawnProcess] = []
    call_count = 0

    async def fake_create_subprocess_exec(
        *_: object,
        **__: object,
    ) -> _StubSpawnProcess:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0)
        if call_count == 1:
            proc = _StubSpawnProcess(pid=12345)
            spawned.append(proc)
            return proc
        raise FileNotFoundError("missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    async def exercise() -> None:
        with pytest.raises(FileNotFoundError):
            await _spawn_pipeline_processes((first, second), config)

    asyncio.run(exercise())

    assert len(spawned) == 1
    assert spawned[0].terminate_calls == 1
    assert spawned[0].kill_calls == 0
    assert spawned[0].wait_calls >= 1


def test_pipeline_requires_at_least_two_stages() -> None:
    """Pipelines reject construction with fewer than two parts."""
    echo = sh.make(ECHO)
    only = echo("-n", "one")

    with pytest.raises(ValueError, match="at least two stages"):
        Pipeline((only,))
