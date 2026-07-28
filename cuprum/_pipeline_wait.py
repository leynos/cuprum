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
    """Exit codes and timing captured once a pipeline finishes waiting."""

    exit_codes: tuple[int, ...]
    failure_index: int | None
    started_at: tuple[float, ...]
    ended_at: tuple[float | None, ...]


@dc.dataclass(slots=True)
class _PipelineWaitState:
    """Mutable bookkeeping for awaiting all stages of a pipeline."""

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
        """Create wait state with one wait task per pipeline process."""
        wait_tasks = [asyncio.create_task(process.wait()) for process in processes]
        return cls(
            wait_tasks=wait_tasks,
            task_to_index={task: idx for idx, task in enumerate(wait_tasks)},
            exit_codes=[None] * len(processes),
            started_at=started_at,
            ended_at=[None] * len(processes),
        )

    def record_completion(
        self,
        completed_idx: int,
        exit_code: int,
        *,
        ended_at: float,
    ) -> None:
        """Record a stage's completion (command).

        This is the pure completion-ordering transition behind
        [`_process_completed_task`][cuprum._pipeline_wait._process_completed_task]:
        it stamps the completed stage's exit code and end time (the clock is
        injected as ``ended_at`` so the transition is deterministic) and latches
        the *first* non-zero exit — in completion order — as ``failure_index``.

        Deciding whether to fail fast is the separate
        [`should_terminate_others`][cuprum._pipeline_wait._PipelineWaitState.should_terminate_others]
        query, and all I/O — reading the clock, terminating stages — stays with
        the caller.

        Examples
        --------
        The first non-zero exit *in completion order* latches, even when a
        lower-indexed stage fails later::

            state = _PipelineWaitState(
                wait_tasks=[],
                task_to_index={},
                exit_codes=[None] * 3,
                started_at=[0.0] * 3,
                ended_at=[None] * 3,
            )
            state.record_completion(2, 0, ended_at=1.0)
            state.record_completion(0, 1, ended_at=2.0)
            state.record_completion(1, 7, ended_at=3.0)

            assert state.failure_index == 0
            assert state.exit_codes == [1, 7, 0]
            assert state.ended_at == [2.0, 3.0, 1.0]

        """
        self.exit_codes[completed_idx] = exit_code
        self.ended_at[completed_idx] = ended_at
        if self.failure_index is None and exit_code != 0:
            self.failure_index = completed_idx

    def should_terminate_others(self, completed_idx: int) -> bool:
        """Report whether completing ``completed_idx`` should fail the pipeline fast.

        This is the query half of the transition: it inspects state without
        changing it, so it is safe to call repeatedly and in any order after
        [`record_completion`][cuprum._pipeline_wait._PipelineWaitState.record_completion]
        has stamped the completion.

        It answers ``True`` exactly when ``completed_idx`` is the latched first
        failure *and* is not the final stage. A failing final stage has nothing
        left to stop, so it never triggers termination. When it does answer
        ``True`` the caller terminates every *other* still-running stage — both
        upstream and downstream — not merely the ones after the failure.

        Examples
        --------
        ::

            state = _PipelineWaitState(
                wait_tasks=[],
                task_to_index={},
                exit_codes=[None] * 3,
                started_at=[0.0] * 3,
                ended_at=[None] * 3,
            )

            state.record_completion(0, 1, ended_at=1.0)
            assert state.should_terminate_others(0) is True

            # A later failure is not the latched first one.
            state.record_completion(1, 1, ended_at=2.0)
            assert state.should_terminate_others(1) is False

        """
        return (
            self.failure_index == completed_idx
            and completed_idx != len(self.exit_codes) - 1
        )


async def _process_completed_task(
    task: asyncio.Task[int],
    state: _PipelineWaitState,
    processes: list[asyncio.subprocess.Process],
    cancel_grace: float,
) -> None:
    """Process a completed wait task, terminating other stages on failure."""
    idx = state.task_to_index[task]
    exit_code = task.result()
    state.record_completion(idx, exit_code, ended_at=time.perf_counter())
    if state.should_terminate_others(idx):
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
