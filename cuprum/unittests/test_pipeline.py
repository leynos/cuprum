"""Unit tests for Pipeline composition and execution."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest

from cuprum import ECHO, scoped, sh
from cuprum._testing import (
    _READ_SIZE,
    _PipelineWaitResult,
    _prepare_pipeline_config,
    _pump_stream,
    _spawn_pipeline_processes,
    _wait_for_pipeline,
)
from cuprum.sh import Pipeline, PipelineResult
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


def _run_test_pipeline(
    stages_exit_codes: list[int],
) -> PipelineResult:
    """Execute a test pipeline with specified per-stage exit codes.

    Parameters
    ----------
    stages_exit_codes:
        Exit code for each pipeline stage.

    Returns
    -------
    PipelineResult
        PipelineResult from synchronous execution.

    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    stages = [
        python("-c", f"import sys; sys.exit({code})") for code in stages_exit_codes
    ]

    if len(stages) < 2:
        msg = "test pipeline helper requires at least two stages"
        raise ValueError(msg)

    pipeline = stages[0] | stages[1]
    for stage in stages[2:]:
        pipeline |= stage

    with scoped(allowlist=frozenset([python_program])):
        return pipeline.run_sync()


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


@pytest.mark.parametrize(
    ("stage_codes", "expect_ok", "expect_failure_index"),
    [
        pytest.param(
            [0, 1],
            False,
            1,
            id="failure-sets-ok-false-and-exposes-failed-stage",
        ),
        pytest.param(
            [0, 0],
            True,
            None,
            id="success-has-no-failure",
        ),
    ],
)
def test_pipeline_run_sync_failure_semantics(
    stage_codes: list[int],
    *,
    expect_ok: bool,
    expect_failure_index: int | None,
) -> None:
    """Validate PipelineResult failure semantics for success and failure cases.

    Tests that:
    - Failed pipelines set ok=False and expose failure/failure_index
    - Successful pipelines set ok=True with failure=None and failure_index=None
    """
    result = _run_test_pipeline(stage_codes)

    assert isinstance(result, PipelineResult)
    assert result.ok is expect_ok
    assert result.failure_index == expect_failure_index
    assert result.final is result.stages[-1]
    assert len(result.stages) == len(stage_codes)

    for idx, expected_code in enumerate(stage_codes):
        assert result.stages[idx].exit_code == expected_code

    if expect_failure_index is not None:
        assert result.failure is result.stages[expect_failure_index]
        assert result.final.exit_code != 0
    else:
        assert result.failure is None
        assert result.final.exit_code == 0


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
    assert (
        writer.write_calls == 2
    )  # drain fails on second write; third chunk never written
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


class _StubPipelineWaitProcess:
    def __init__(
        self,
        *,
        pid: int,
        exit_code: int,
        ready: asyncio.Event | None = None,
    ) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = None
        self.stderr = None
        self.stdin = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self._exit_code = exit_code
        self._ready = ready

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._exit_code = -15
        if self._ready is not None:
            self._ready.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self._exit_code = -9
        if self._ready is not None:
            self._ready.set()

    async def wait(self) -> int:
        if self._ready is not None:
            await self._ready.wait()
        await asyncio.sleep(0)
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode


async def _exercise_wait_for_pipeline(
    exit_codes: tuple[int, int, int],
    ready_stages: frozenset[int],
) -> tuple[
    _StubPipelineWaitProcess,
    _StubPipelineWaitProcess,
    _StubPipelineWaitProcess,
    _PipelineWaitResult,
]:
    """Execute _wait_for_pipeline with stub processes and custom exit scenarios.

    Parameters
    ----------
    exit_codes:
        Exit code for each of the three stages.
    ready_stages:
        Set of stage indices that should be immediately ready.

    Returns
    -------
    tuple
        Tuple of (process0, process1, process2, wait_result).

    """
    events = [asyncio.Event() for _ in range(3)]
    for idx in ready_stages:
        events[idx].set()

    processes = [
        _StubPipelineWaitProcess(pid=i + 1, exit_code=exit_codes[i], ready=events[i])
        for i in range(3)
    ]

    result = await _wait_for_pipeline(
        typ.cast("list[asyncio.subprocess.Process]", processes),
        pipe_tasks=[],
        cancel_grace=0.01,
        started_at=[0.0, 0.0, 0.0],
    )

    return processes[0], processes[1], processes[2], result


def _assert_stage_terminated(
    process: _StubPipelineWaitProcess,
    *,
    should_terminate: bool,
) -> None:
    """Assert whether a process was terminated during fail-fast."""
    if should_terminate:
        assert process.terminate_calls == 1, (
            f"Process {process.pid} should be terminated"
        )
    else:
        assert process.terminate_calls == 0, (
            f"Process {process.pid} should not be terminated"
        )


def _assert_pipeline_failure(
    result: _PipelineWaitResult,
    *,
    failure_index: int | None,
    exit_codes: tuple[int, ...],
) -> None:
    """Assert pipeline wait result failure metadata."""
    assert result.failure_index == failure_index
    assert result.exit_codes == exit_codes


@dc.dataclass(frozen=True)
class _FailFastScenario:
    """Configuration for a fail-fast pipeline test scenario."""

    exit_codes: tuple[int, int, int]
    ready_stages: frozenset[int]
    expected_failure_index: int
    expected_exit_codes: tuple[int, int, int]
    terminated_stages: frozenset[int]


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(
            _FailFastScenario(
                exit_codes=(7, 0, 0),
                ready_stages=frozenset([0]),
                expected_failure_index=0,
                expected_exit_codes=(7, -15, -15),
                terminated_stages=frozenset([1, 2]),
            ),
            id="early-stage-failure-terminates-downstream",
        ),
        pytest.param(
            _FailFastScenario(
                exit_codes=(0, 3, 0),
                ready_stages=frozenset([1]),
                expected_failure_index=1,
                expected_exit_codes=(-15, 3, -15),
                terminated_stages=frozenset([0, 2]),
            ),
            id="middle-stage-failure-terminates-downstream",
        ),
        pytest.param(
            _FailFastScenario(
                exit_codes=(0, 0, 5),
                ready_stages=frozenset([0, 1, 2]),
                expected_failure_index=2,
                expected_exit_codes=(0, 0, 5),
                terminated_stages=frozenset(),
            ),
            id="last-stage-failure-no-termination",
        ),
    ],
)
def test_wait_for_pipeline_fail_fast_scenarios(
    scenario: _FailFastScenario,
) -> None:
    """Validate fail-fast termination behaviour across different failure scenarios.

    Tests that:
    - Early stage failures terminate all downstream stages
    - Middle stage failures terminate all downstream stages
    - Final stage failures record failure index without terminating others
    """
    p0, p1, p2, result = asyncio.run(
        _exercise_wait_for_pipeline(
            exit_codes=scenario.exit_codes,
            ready_stages=scenario.ready_stages,
        ),
    )

    _assert_pipeline_failure(
        result,
        failure_index=scenario.expected_failure_index,
        exit_codes=scenario.expected_exit_codes,
    )

    for idx, process in enumerate([p0, p1, p2]):
        _assert_stage_terminated(
            process,
            should_terminate=(idx in scenario.terminated_stages),
        )
