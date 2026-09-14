"""Unit tests for pipeline process spawning and cleanup on failure."""

from __future__ import annotations

import asyncio

import pytest

from cuprum import ECHO, _pipeline_stage_streams, _process_lifecycle, sh
from cuprum._testing import _prepare_pipeline_config, _spawn_pipeline_processes
from cuprum.sh import RunOutputOptions


class _StubSpawnProcess:
    """Stub subprocess recording terminate, kill, and wait calls."""

    def __init__(self, pid: int) -> None:
        """Initialize the stub process with the given PID."""
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = None
        self.stderr = None
        self.stdin = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def terminate(self) -> None:
        """Record that the process was terminated."""
        self.terminate_calls += 1

    def kill(self) -> None:
        """Record that the process was killed."""
        self.kill_calls += 1

    async def wait(self) -> int:
        """Record the wait and return a default terminated exit code."""
        self.wait_calls += 1
        await asyncio.sleep(0)
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


def test_spawn_pipeline_processes_terminates_started_stages_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawn failures should terminate any already-started pipeline stages."""
    echo = sh.make(ECHO)
    first = echo("-n", "hello")
    second = echo("-n", "world")
    config = _prepare_pipeline_config(
        output=RunOutputOptions(capture=True, echo=False),
        timeout=None,
        context=None,
    )

    spawned: list[_StubSpawnProcess] = []
    call_count = 0

    async def fake_create_subprocess_exec(
        *_: object,
        **__: object,
    ) -> _StubSpawnProcess:
        """Spawn the first stage, then fail subsequent spawn attempts."""
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0)
        if call_count == 1:
            proc = _StubSpawnProcess(pid=12345)
            spawned.append(proc)
            return proc
        message = "missing"
        raise FileNotFoundError(message)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    async def exercise() -> None:
        """Spawn the pipeline and assert the spawn failure propagates."""
        with pytest.raises(FileNotFoundError):
            await _spawn_pipeline_processes((first, second), config)

    asyncio.run(exercise())

    assert len(spawned) == 1, (
        "only the first stage should have been spawned before the failure"
    )
    assert spawned[0].terminate_calls == 1, "the spawned stage must be terminated once"
    assert spawned[0].kill_calls == 0, (
        "a cooperative stage must not need escalation to kill"
    )
    assert spawned[0].wait_calls >= 1, "the terminated stage must be awaited"


def test_spawn_pipeline_processes_records_times_before_stage_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each stage samples both clocks before awaiting its subprocess spawn."""
    events: list[str] = []

    def monotonic_clock() -> float:
        """Record the monotonic stage-start sample."""
        events.append("monotonic")
        return 10.0

    def wall_clock() -> float:
        """Record the wall-clock stage-start sample."""
        events.append("wall")
        return 20.0

    async def fake_create_subprocess_exec(
        *_: object,
        **__: object,
    ) -> _StubSpawnProcess:
        """Assert that both start-time samples precede stage spawning."""
        events.append("spawn")
        assert events == ["monotonic", "wall", "spawn"]
        await asyncio.sleep(0)
        return _StubSpawnProcess(pid=12345)

    def fake_create_stage_capture_tasks(
        *_: object,
        **__: object,
    ) -> tuple[None, None]:
        """Avoid stream-task setup in this spawn-boundary test."""
        return None, None

    config = _prepare_pipeline_config(
        output=RunOutputOptions(capture=False, echo=False),
        timeout=None,
        context=None,
    )
    monkeypatch.setattr(_process_lifecycle.time, "perf_counter", monotonic_clock)
    monkeypatch.setattr(sh.time, "time", wall_clock)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        _pipeline_stage_streams,
        "_create_stage_capture_tasks",
        fake_create_stage_capture_tasks,
    )

    *_, started_at, wall_clock_started_at = asyncio.run(
        _spawn_pipeline_processes((sh.make(ECHO)("quiet"),), config),
    )

    assert started_at == [10.0]
    assert wall_clock_started_at == [20.0]
