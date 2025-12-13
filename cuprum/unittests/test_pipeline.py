"""Unit tests for Pipeline composition and execution."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ECHO, scoped, sh
from cuprum.sh import _READ_SIZE, Pipeline, PipelineResult, _pump_stream
from tests.helpers.catalogue import python_catalogue


def test_or_operator_composes_pipeline() -> None:
    """The | operator composes SafeCmd stages into a Pipeline."""
    echo = sh.make(ECHO)
    first = echo("-n", "hello")
    second = echo("-n", "world")

    pipeline = first | second

    assert isinstance(pipeline, Pipeline)
    assert pipeline.parts == (first, second)


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


def test_pump_stream_drains_per_chunk() -> None:
    """Streaming between stages awaits drain for backpressure."""

    class StubWriter:
        def __init__(self) -> None:
            self.data = bytearray()
            self.drain_calls = 0
            self.write_calls = 0
            self.closed = False
            self.write_eof_calls = 0

        def write(self, chunk: bytes) -> None:
            self.write_calls += 1
            self.data.extend(chunk)

        async def drain(self) -> None:
            self.drain_calls += 1
            await asyncio.sleep(0)

        def write_eof(self) -> None:
            self.write_eof_calls += 1

        def close(self) -> None:
            self.closed = True

    async def exercise() -> StubWriter:
        reader = asyncio.StreamReader()
        writer = StubWriter()
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


def test_pipeline_requires_at_least_two_stages() -> None:
    """Pipelines reject construction with fewer than two parts."""
    echo = sh.make(ECHO)
    only = echo("-n", "one")

    with pytest.raises(ValueError, match="at least two stages"):
        Pipeline((only,))
