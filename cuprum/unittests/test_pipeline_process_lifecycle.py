"""Unit tests for pipeline process spawning and cleanup on failure."""

from __future__ import annotations

import asyncio

import pytest

from cuprum import ECHO, sh
from cuprum._pipeline_config import _PipelineStreamOptions
from cuprum._testing import _prepare_pipeline_config, _spawn_pipeline_processes


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
        capture=True,
        output=_PipelineStreamOptions(echo_stdout=False, echo_stderr=False),
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
