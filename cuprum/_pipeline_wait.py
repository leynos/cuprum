"""Await a pipeline's stages and decide which completion triggers fail-fast.

The fail-fast decision is split from the asyncio machinery that observes it, so
the ordering rules can be verified without processes or a clock.
``_PipelineWaitState`` carries the bookkeeping and exposes the decision as a
command and a query, per the project's command/query segregation rule:

- ``record_completion`` stamps a stage's exit code and injected end time, and
  latches the first non-zero exit **in completion order** as ``failure_index``.
  Completion order decides, not stage order.
- ``should_terminate_others`` reports, without mutating anything, whether that
  completion should stop every other still-running stage.

``_process_completed_task`` is the only place the two are joined: it reads the
clock, applies the command, emits the structured fail-fast records, and acts on
the query. Keeping the I/O there leaves the ordering rules as a pure transition
that Hypothesis and CrossHair drive directly.

Work that belongs to neighbouring modules rather than here: terminating and
cleaning up processes lives in ``cuprum._process_lifecycle``
(``_terminate_pipeline_remaining_stages``, ``_cleanup_pipeline_on_error``), and
collecting the inter-stage pipe task outcomes lives in
``cuprum._pipeline_streams`` (``_collect_pipe_results``,
``_surface_unexpected_pipe_failures``). This module owns only the waiting and
the ordering decision.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
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

_LOGGER = logging.getLogger(__name__)


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


@dc.dataclass(frozen=True, slots=True)
class _CompletionLogFields:
    """Structured fields shared by the pipeline-wait completion records."""

    stage_index: int
    exit_code: int
    duration_s: float


def _completion_log_fields(
    state: _PipelineWaitState,
    completed_idx: int,
    exit_code: int,
    ended_at: float,
) -> _CompletionLogFields:
    """Derive the log fields for a completion, including elapsed time.

    The duration comes from the injected completion time and the stage's
    recorded start, clamped at zero so a non-monotonic clock reading cannot
    report a negative elapsed time.
    """
    return _CompletionLogFields(
        stage_index=completed_idx,
        exit_code=exit_code,
        duration_s=max(0.0, ended_at - state.started_at[completed_idx]),
    )


def _log_completion_event(
    action: str,
    message: str,
    fields: _CompletionLogFields,
) -> None:
    """Emit one structured pipeline-wait record with stable ``cuprum_`` keys.

    ``action`` is the stable event name operators filter on, so the two
    completion records — latching the first failure and starting fail-fast
    termination — stay distinguishable even though they share their fields.
    """
    _LOGGER.warning(
        message,
        fields.stage_index,
        fields.exit_code,
        extra={
            "cuprum_action": action,
            "cuprum_stage_index": fields.stage_index,
            "cuprum_exit_code": fields.exit_code,
            "cuprum_duration_s": fields.duration_s,
        },
    )


async def _process_completed_task(
    task: asyncio.Task[int],
    state: _PipelineWaitState,
    processes: list[asyncio.subprocess.Process],
    cancel_grace: float,
) -> None:
    """Process a completed wait task, terminating other stages on failure.

    This is where the runtime concerns live: reading the clock, invoking the
    pure command, acting on the pure query, and emitting the structured records
    that make fail-fast behaviour observable. Logging stays here deliberately —
    `record_completion` and `should_terminate_others` must remain a
    side-effect-free mutation and query so they can be verified symbolically.
    """
    idx = state.task_to_index[task]
    exit_code = task.result()
    ended_at = time.perf_counter()

    # Captured before the command so this completion can be distinguished from
    # one that merely follows an already-latched failure.
    had_failure = state.failure_index is not None
    state.record_completion(idx, exit_code, ended_at=ended_at)
    latched_first_failure = not had_failure and state.failure_index == idx

    fields = _completion_log_fields(state, idx, exit_code, ended_at)

    if latched_first_failure:
        _log_completion_event(
            "pipeline_stage_first_failure",
            "pipeline stage %d exited %d, latching first failure",
            fields,
        )

    if state.should_terminate_others(idx):
        _log_completion_event(
            "pipeline_fail_fast_termination",
            "terminating other pipeline stages after stage %d exited %d",
            fields,
        )
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
