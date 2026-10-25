"""Support the native-worker lifecycle fixtures without racing startup deadlines."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import threading
import time
import typing as typ

from cuprum import (
    TimeoutExpired,
    _pipeline_collect,
    _pipeline_native_pump_runtime,
    _pipeline_streams,
    sh,
)
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import pytest

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._pipeline_records import _PipelineSpawnResult
    from cuprum._pipeline_wait import _PipelineWaitResult
    from cuprum.program import Program
    from cuprum.sh import Pipeline


_SYNC_PIPELINE_TIMEOUT_S = 0.05
_SYNC_PIPELINE_CLEANUP_GRACE_S = 0.01
_SYNC_PIPELINE_RETURN_BOUND_S = (
    _SYNC_PIPELINE_TIMEOUT_S + _SYNC_PIPELINE_CLEANUP_GRACE_S + 0.75
)


class _RecordingGuard:
    """Blocking-mode guard double that signals fallback completion."""

    def __init__(self, restored: threading.Event) -> None:
        """Retain the completion signal for the loop-closure assertion."""
        self.restored = restored

    def restore(self) -> None:
        """Record that fallback cleanup restored descriptor state."""
        self.restored.set()


@dc.dataclass(slots=True)
class _ExecutorLifecycle:
    """Synchronization and descriptor evidence for one late worker."""

    worker_started: threading.Event = dc.field(default_factory=threading.Event)
    release_worker: threading.Event = dc.field(default_factory=threading.Event)
    worker_finished: threading.Event = dc.field(default_factory=threading.Event)
    restored: threading.Event = dc.field(default_factory=threading.Event)
    native_fds: list[tuple[int, int]] = dc.field(default_factory=list)


@dc.dataclass(slots=True)
class _SyncPipelineRun:
    """Capture one synchronous pipeline result from its caller thread."""

    completed: threading.Event = dc.field(default_factory=threading.Event)
    errors: list[BaseException] = dc.field(default_factory=list)
    rust_attempts: list[None] = dc.field(default_factory=list)


def _assert_sync_timeout_returned(
    caller: threading.Thread,
    sync_run: _SyncPipelineRun,
    started_at: float,
) -> None:
    """Assert the public caller returns before its held worker is released."""
    caller.join(timeout=_SYNC_PIPELINE_RETURN_BOUND_S)
    elapsed_s = time.monotonic() - started_at
    assert not caller.is_alive(), (
        "run_sync must return after timeout plus cleanup grace rather than "
        f"wait for native I/O; elapsed {elapsed_s:.6f}s"
    )
    assert sync_run.completed.is_set(), "run_sync must publish its outcome"
    assert len(sync_run.errors) == 1, (
        f"run_sync must raise one public error, found {sync_run.errors!r}"
    )
    assert isinstance(sync_run.errors[0], TimeoutExpired), (
        f"run_sync must raise TimeoutExpired, found {sync_run.errors!r}"
    )


def _wait_for_deferred_cleanup() -> bool:
    """Wait for the completion callback to discard its retained worker future."""
    deadline = time.monotonic() + 5.0
    while _pipeline_native_pump_runtime._DEFAULT_NATIVE_PUMP_RUNTIME.retained_futures:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _arm_timeout_after_worker_starts(
    monkeypatch: pytest.MonkeyPatch,
    worker_started: threading.Event,
) -> None:
    """Apply the configured elapsed deadline only after native-worker readiness."""
    original_wait = _pipeline_collect._await_pipeline_wait_result

    async def wait_after_readiness(
        spawn: _PipelineSpawnResult,
        config: _PipelineRunConfig,
        *,
        timeout_deadline: float | None,
        pipe_tasks: list[asyncio.Task[None]],
    ) -> _PipelineWaitResult:
        """Let startup finish before exercising the real timeout cleanup path."""
        assert timeout_deadline is not None, "the fixture requires a deadline"
        assert config.timeout is not None, "the fixture requires an elapsed timeout"
        started = await asyncio.to_thread(worker_started.wait, 5.0)
        assert started, "the native worker must start before arming the deadline"
        # These fixtures exercise a held worker's teardown, not startup latency.
        # Keep the real wait and its original timeout duration after the barrier.
        return await original_wait(
            spawn,
            config,
            timeout_deadline=time.monotonic() + config.timeout,
            pipe_tasks=pipe_tasks,
        )

    monkeypatch.setattr(
        _pipeline_collect, "_await_pipeline_wait_result", wait_after_readiness
    )


def _delay_pump_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    startup_delay_s: float,
) -> None:
    """Model startup later than the elapsed deadline without blocking the loop."""
    original_dispatch = _pipeline_streams._pump_stream_dispatch

    async def delayed_dispatch(
        reader: asyncio.StreamReader | None,
        writer: asyncio.StreamWriter | None,
        *,
        cleanup_grace_s: float,
        read_size: int | None,
    ) -> None:
        """Delay the handoff while preserving every original dispatch argument."""
        await asyncio.sleep(startup_delay_s)
        await original_dispatch(
            reader,
            writer,
            cleanup_grace_s=cleanup_grace_s,
            read_size=read_size,
        )

    monkeypatch.setattr(_pipeline_streams, "_pump_stream_dispatch", delayed_dispatch)


def _make_sync_timeout_pipeline() -> tuple[Pipeline, frozenset[Program]]:
    """Keep both stages alive through native handoff until pipeline timeout."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    # An exited upstream closes its reader before delayed dispatch can borrow
    # the descriptor, so both stages must outlive the native handoff.
    pipeline = python(
        "-c", "import time; print('payload', flush=True); time.sleep(60)"
    ) | python("-c", "import time; time.sleep(60)")
    return pipeline, frozenset((python_program,))
