"""Pipeline completion and collection debit one injected monotonic clock."""

from __future__ import annotations

import asyncio
import io
import types
import typing as typ

import pytest

from cuprum import _pipeline_collect
from cuprum._pipeline_config import _PipelineRunConfig
from cuprum._pipeline_records import (
    _PipelineSpawnResult,
    _PipelineWaitResult,
    _StageWaitContext,
)
from cuprum._sink_lifecycle import _SinkBracket
from cuprum.sh import ExecutionContext
from cuprum.unittests._stream_drain_support import captured

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation
    from cuprum._stream_drain_finish import _CapturedDrain
    from cuprum.sh import SafeCmd


async def _reaped_stages(*_args: object, **_kwargs: object) -> _PipelineWaitResult:  # ruff: ignore[unused-async]
    """Report already-reaped stages without consuming wall-clock time."""
    return _PipelineWaitResult((), None, (), (), ())


def _patch_wait_recorders(
    monkeypatch: pytest.MonkeyPatch,
    waits: list[float | None],
) -> None:
    """Record each wait budget while retaining asyncio's real task scheduling."""
    real_wait_for = asyncio.wait_for
    real_wait = asyncio.wait

    async def wait_for[T](awaitable: cabc.Awaitable[T], delay: float) -> T:
        """Record the stage wait's remaining deadline."""
        waits.append(delay)
        return await real_wait_for(awaitable, delay)

    async def wait(
        tasks: cabc.Iterable[asyncio.Task[_CapturedDrain]],
        *,
        timeout: float | None,  # ruff: ignore[async-function-with-timeout] - mirrors asyncio.wait's patch seam.
        return_when: str,
    ) -> tuple[set[asyncio.Task[_CapturedDrain]], set[asyncio.Task[_CapturedDrain]]]:
        """Record the later consumer wait's remainder."""
        waits.append(timeout)
        return await real_wait(tasks, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    monkeypatch.setattr(asyncio, "wait", wait)
    monkeypatch.setattr(_pipeline_collect, "_wait_for_pipeline", _reaped_stages)


def _config(timeout: float | None) -> _PipelineRunConfig:
    """Build the non-echoing collection configuration under test."""
    return _PipelineRunConfig(
        ctx=ExecutionContext(),
        capture_stdout=None,
        capture_stderr=None,
        echo_stdout=False,
        echo_stderr=False,
        max_echo_line_bytes=None,
        timeout=timeout,
        stdout_sink=io.StringIO(),
        stderr_sink=io.StringIO(),
        sink_bracket=_SinkBracket(None),
    )


async def _collect_completed_output(
    config: _PipelineRunConfig,
    observations: tuple[_StageObservation, ...],
) -> _CapturedDrain | None:
    """Collect one completed consumer through the real collection boundary."""
    consumer = asyncio.create_task(asyncio.sleep(0, result=captured("kept")))
    await consumer
    spawn = _PipelineSpawnResult(
        processes=[],
        stderr_tasks=[],
        stdout_task=consumer,
        relay_diagnostics_by_stage=(),
        stages=_StageWaitContext((), observations=observations),
    )
    result = await _pipeline_collect._collect_pipeline_inputs(
        typ.cast("tuple[SafeCmd, ...]", ()),
        spawn,
        config,
    )
    return result.final_stdout


@pytest.mark.parametrize("timeout", [10.0, None])
@pytest.mark.parametrize(
    "has_observation", [True, False], ids=["observation", "fallback"]
)
def test_collection_uses_the_observation_clock_for_every_deadline_read(
    monkeypatch: pytest.MonkeyPatch,
    timeout: float | None,
    *,
    has_observation: bool,
) -> None:
    """Stage waiting and output waiting share the original absolute deadline."""
    readings = iter([100.0, 103.0, 108.0] if timeout is not None else [])
    waits: list[float | None] = []

    def clock() -> float:
        """Fail if an unbounded pipeline unexpectedly reads a deadline clock."""
        return next(readings)

    def fallback_clock() -> float:
        """Allow fallback reads only when no observation supplied a clock."""
        assert not has_observation, "an observed pipeline must use its injected clock"
        return clock()

    _patch_wait_recorders(monkeypatch, waits)
    # Changing time.monotonic globally would also replace the event loop's
    # scheduling clock, so only the collection module gets the fallback double.
    monkeypatch.setattr(
        _pipeline_collect, "time", types.SimpleNamespace(monotonic=fallback_clock)
    )
    observation = typ.cast(
        "_StageObservation", types.SimpleNamespace(monotonic_clock=clock)
    )
    output = asyncio.run(
        _collect_completed_output(
            _config(timeout),
            (observation,) if has_observation else (),
        )
    )
    assert output is not None
    assert output.payload == "kept"
    expected = [7.0, 2.0] if timeout is not None else [None]
    assert waits == expected, (
        f"both waits must debit the same injected clock: {waits!r} != {expected!r}"
    )
