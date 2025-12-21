"""Pipeline waiting logic with fail-fast semantics."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import time
import typing as typ

from cuprum._pipeline_streams import (
    _collect_pipe_results,
    _surface_unexpected_pipe_failures,
)
from cuprum._process_lifecycle import (
    _cleanup_pipeline_on_error,
    _terminate_pipeline_remaining_stages,
)


@dc.dataclass(frozen=True, slots=True)
class _PipelineWaitResult:
    exit_codes: tuple[int, ...]
    failure_index: int | None
    started_at: tuple[float, ...]
    ended_at: tuple[float | None, ...]


@dc.dataclass(slots=True)
class _PipelineWaitState:
    wait_tasks: list[asyncio.Task[int]]
    task_to_index: dict[asyncio.Task[int], int]
    exit_codes: list[int | None]
    started_at: list[float]
    ended_at: list[float | None]
    failure_index: int | None = None

    @classmethod
    def from_processes(
        cls,
        processes: list[asyncio.subprocess.Process],
        *,
        started_at: list[float],
    ) -> _PipelineWaitState:
        wait_tasks = [asyncio.create_task(process.wait()) for process in processes]
        return cls(
            wait_tasks=wait_tasks,
            task_to_index={task: idx for idx, task in enumerate(wait_tasks)},
            exit_codes=[None] * len(processes),
            started_at=started_at,
            ended_at=[None] * len(processes),
        )


async def _process_completed_task(
    task: asyncio.Task[int],
    state: _PipelineWaitState,
    processes: list[asyncio.subprocess.Process],
    cancel_grace: float,
) -> None:
    """Process a completed wait task, terminating remaining stages on failure."""
    idx = state.task_to_index[task]
    exit_code = task.result()
    state.exit_codes[idx] = exit_code
    state.ended_at[idx] = time.perf_counter()
    if state.failure_index is None and exit_code != 0:
        state.failure_index = idx
        if idx != len(processes) - 1:
            await _terminate_pipeline_remaining_stages(
                processes,
                state.wait_tasks,
                idx,
                cancel_grace=cancel_grace,
            )


async def _finalize_pipeline_wait(
    pipe_tasks: list[asyncio.Task[None]],
    pipe_results: list[object] | None,
    caught: BaseException | None,
) -> list[object]:
    """Collect pipe results and surface unexpected failures when appropriate."""
    if pipe_results is None:
        pipe_results = await _collect_pipe_results(pipe_tasks)
    if caught is None:
        _surface_unexpected_pipe_failures(pipe_results)
    return pipe_results


async def _wait_for_pipeline(
    processes: list[asyncio.subprocess.Process],
    *,
    pipe_tasks: list[asyncio.Task[None]],
    cancel_grace: float,
    started_at: list[float],
) -> _PipelineWaitResult:
    """Wait for pipeline completion, ensuring subprocess cleanup on cancellation."""
    state = _PipelineWaitState.from_processes(processes, started_at=started_at)

    caught: BaseException | None = None
    pipe_results: list[object] | None = None
    try:
        pending = set(state.wait_tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for wait_task in done:
                await _process_completed_task(
                    typ.cast("asyncio.Task[int]", wait_task),
                    state,
                    processes,
                    cancel_grace,
                )

        completed_exit_codes = tuple(
            -1 if code is None else code for code in state.exit_codes
        )
        return _PipelineWaitResult(
            exit_codes=completed_exit_codes,
            failure_index=state.failure_index,
            started_at=tuple(state.started_at),
            ended_at=tuple(state.ended_at),
        )
    except BaseException as exc:
        caught = exc
        pipe_results = await _cleanup_pipeline_on_error(
            processes,
            pipe_tasks,
            cancel_grace,
        )
        await asyncio.gather(*state.wait_tasks, return_exceptions=True)
        raise
    finally:
        pipe_results = await _finalize_pipeline_wait(pipe_tasks, pipe_results, caught)
