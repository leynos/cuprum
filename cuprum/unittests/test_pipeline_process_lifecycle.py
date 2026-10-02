"""Unit tests for pipeline process spawning and cleanup on failure."""

from __future__ import annotations

import asyncio

import pytest

from cuprum import (
    ECHO,
    _pipeline_internals,
    _pipeline_spawn,
    _pipeline_stage_streams,
    sh,
)
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


class _StagePreparationError(RuntimeError):
    """A failure raised while preparing a stage's line callbacks."""


def test_stage_preparation_failure_reaps_every_spawned_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after a stage has spawned must not orphan that stage.

    ``_create_stage_capture_tasks`` is the factory the line-event hoist
    changes, and it runs *after* its stage's process exists, so a failure there
    leaves a live child unless the owner reclaims it. Two stages spawn and the
    second stage's factory raises, which exercises both halves of the cleanup:
    the earlier stage's capture task is cancelled, and every spawned process —
    the failing stage's included — is terminated and awaited.

    This pins the pipeline path only. The two command paths leak the child on
    the same injection; that gap is recorded as contradicted in the plan and
    deliberately has no test here.
    """
    echo = sh.make(ECHO)
    stages = (echo("-n", "first"), echo("-n", "second"))
    config = _prepare_pipeline_config(
        output=RunOutputOptions(capture=True, echo=False),
        timeout=None,
        context=None,
    )

    spawned: list[_StubSpawnProcess] = []
    prepared: list[asyncio.Task[str | None]] = []
    factory_calls = 0

    async def never_completes() -> str | None:
        """Suspend until the cleanup cancels this capture task."""
        await asyncio.sleep(3600)
        return None

    async def fake_create_subprocess_exec(
        *_: object,
        **__: object,
    ) -> _StubSpawnProcess:
        """Spawn every stage, recording each one with its own PID."""
        await asyncio.sleep(0)
        proc = _StubSpawnProcess(pid=12345 + len(spawned))
        spawned.append(proc)
        return proc

    def fake_create_stage_capture_tasks(
        *_: object,
        **__: object,
    ) -> tuple[
        asyncio.Task[str | None] | None,
        asyncio.Task[str | None] | None,
        tuple[None, None],
    ]:
        """Hand the first stage a live capture task; fail the second stage."""
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls > 1:
            raise _StagePreparationError
        task = asyncio.create_task(never_completes())
        prepared.append(task)
        return task, None, (None, None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        _pipeline_stage_streams,
        "_create_stage_capture_tasks",
        fake_create_stage_capture_tasks,
    )

    async def exercise() -> None:
        """Spawn the pipeline and assert the preparation failure propagates."""
        with pytest.raises(_StagePreparationError):
            await _spawn_pipeline_processes(stages, config)

    asyncio.run(exercise())

    # Anti-vacuity: a test that never reached the second stage could assert
    # the same things about one process and read as a pass.
    assert len(spawned) == len(stages), (
        "both stages must spawn before the second stage's factory fails"
    )
    assert [proc.terminate_calls for proc in spawned] == [1, 1], (
        "every spawned stage must be terminated, the failing stage included"
    )
    assert [proc.kill_calls for proc in spawned] == [0, 0], (
        "a cooperative stage must not need escalation to kill"
    )
    assert all(proc.wait_calls >= 1 for proc in spawned), (
        "every terminated stage must be awaited"
    )
    assert len(prepared) == 1, "the first stage must have produced a capture task"
    assert prepared[0].cancelled(), (
        "the earlier stage's capture task must be cancelled by the cleanup"
    )


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
        assert events == ["monotonic", "wall", "spawn"], (
            "stage start clocks must be sampled before subprocess spawn"
        )
        await asyncio.sleep(0)
        return _StubSpawnProcess(pid=12345)

    def fake_create_stage_capture_tasks(
        *_: object,
        **__: object,
    ) -> tuple[None, None, tuple[None, None]]:
        """Avoid stream-task and relay-collector setup in this spawn-boundary test."""
        return None, None, (None, None)

    config = _prepare_pipeline_config(
        output=RunOutputOptions(capture=False, echo=False),
        timeout=None,
        context=None,
    )
    monkeypatch.setattr(_pipeline_spawn.time, "perf_counter", monotonic_clock)
    # The stage observation builder installs ``time.time`` as each stage's
    # ``wall_clock`` callable, so the injected wall clock must be patched where
    # that attribute is read from — not in ``sh``, which no longer imports
    # ``time`` now that observation construction lives in ``_pipeline_internals``.
    monkeypatch.setattr(_pipeline_internals.time, "time", wall_clock)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        _pipeline_stage_streams,
        "_create_stage_capture_tasks",
        fake_create_stage_capture_tasks,
    )

    # The trailing relay-diagnostics list is unused here, but names the last
    # element so the two clock lists keep binding to their own fields.
    *_, started_at, wall_clock_started_at, _relay_diagnostics = asyncio.run(
        _spawn_pipeline_processes((sh.make(ECHO)("quiet"),), config),
    )

    assert started_at == [10.0], "pipeline must return each monotonic stage start"
    assert wall_clock_started_at == [20.0], (
        "pipeline must return each wall-clock stage start"
    )
