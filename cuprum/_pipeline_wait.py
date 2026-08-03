"""Await a pipeline's stages and decide which completion triggers fail-fast.

The fail-fast decision is split from the asyncio machinery that observes it, so
the ordering rules can be verified without processes or a clock.
``_PipelineWaitState`` carries the bookkeeping and exposes the decision as a
command and a query, per the project's command/query segregation rule:

- ``record_completion`` stamps a stage's exit code and injected end time, and
  latches the first non-zero exit **in completion order** as ``failure_index``.
  Completion order decides, not stage order. Stages that complete together are
  the one case order cannot separate: ``asyncio.wait`` hands back an unordered
  set, so ``_wait_for_pipeline`` feeds a batch through in stage order, making
  the earliest stage — the upstream one that caused the rest — the reported
  failure rather than whichever the set yielded first.
- ``should_terminate_others`` reports, without mutating anything, whether that
  completion should stop every other still-running stage.

``_process_completed_task`` is the only place the two are joined: it reads the
clock, applies the command, publishes the fail-fast report — the structured log
records and the ``pipeline_fail_fast`` observe event — and acts on the query.
Keeping the I/O there leaves the ordering rules as a pure transition that
Hypothesis and CrossHair drive directly.

Work that belongs to neighbouring modules rather than here: terminating and
cleaning up processes lives in ``cuprum._process_lifecycle``
(``_terminate_pipeline_remaining_stages``, ``_cleanup_pipeline_on_error``),
collecting the inter-stage pipe task outcomes lives in
``cuprum._pipeline_streams`` (``_collect_pipe_results``,
``_surface_unexpected_pipe_failures``), and the shape of what the fail-fast
path publishes lives in ``cuprum._pipeline_wait_records``. This module owns
only the waiting, the ordering decision, and when each report fires.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import time
import typing as typ

from cuprum._pipeline_streams import (
    _collect_pipe_results,
    _surface_unexpected_pipe_failures,
)
from cuprum._pipeline_wait_records import (
    completion_log_fields,
    emit_fail_fast_event,
    log_completion_event,
)
from cuprum._process_lifecycle import (
    _cleanup_pipeline_on_error,
    _terminate_pipeline_remaining_stages,
)

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation, _StageWaitContext
    from cuprum._pipeline_wait_records import _CompletionLogFields


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
    # Reporting only: the completion transition never reads either of these,
    # which is why both default to empty and the symbolic model leaves them so.
    # ``exec_ids`` labels the log records; ``observations`` is what the
    # fail-fast ``ExecEvent`` is published through.
    exec_ids: tuple[str, ...] = ()
    observations: tuple[_StageObservation, ...] = ()

    @classmethod
    def from_processes(
        cls,
        processes: list[asyncio.subprocess.Process],
        *,
        stages: _StageWaitContext,
    ) -> _PipelineWaitState:
        """Create wait state with one wait task per pipeline process."""
        wait_tasks = [asyncio.create_task(process.wait()) for process in processes]
        return cls(
            wait_tasks=wait_tasks,
            task_to_index={task: idx for idx, task in enumerate(wait_tasks)},
            exit_codes=[None] * len(processes),
            started_at=stages.started_at,
            ended_at=[None] * len(processes),
            exec_ids=stages.exec_ids,
            observations=stages.observations,
        )

    def exec_id(self, stage_index: int) -> str | None:
        """Return a stage's correlation token, or ``None`` when unknown.

        Absent only when the state was built without observation context, as
        the symbolic model and the transition-level tests do; a pipeline run
        through `_wait_for_pipeline` always supplies one token per stage.
        """
        if stage_index < len(self.exec_ids):
            return self.exec_ids[stage_index]
        return None

    def observation(self, stage_index: int) -> _StageObservation | None:
        """Return a stage's observation, or ``None`` when there is none.

        Absent under the same conditions as `exec_id`, and additionally
        harmless: with no observation there is no hook set to publish the
        fail-fast event to.
        """
        if stage_index < len(self.observations):
            return self.observations[stage_index]
        return None

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


async def _terminate_and_report(
    state: _PipelineWaitState,
    processes: list[asyncio.subprocess.Process],
    cancel_grace: float,
    fields: _CompletionLogFields,
) -> None:
    """Terminate the other stages, reporting the teardown either side of it.

    The starting record alone cannot distinguish a teardown that finished from
    one still waiting on a stage that will not die, so the outcome — how many
    stages were actually stopped, and how long the teardown took — is reported
    once termination returns.
    """
    log_completion_event(
        "pipeline_fail_fast_termination",
        "terminating other pipeline stages after stage %d exited %d",
        fields,
    )
    started = time.perf_counter()
    terminated = await _terminate_pipeline_remaining_stages(
        processes,
        state.wait_tasks,
        fields.stage_index,
        cancel_grace=cancel_grace,
    )
    log_completion_event(
        "pipeline_fail_fast_terminated",
        "terminated other pipeline stages after stage %d exited %d",
        fields,
        cuprum_terminated_stage_count=terminated,
        cuprum_termination_duration_s=max(0.0, time.perf_counter() - started),
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

    terminate_others = state.should_terminate_others(idx)

    # Every stage passes through here, and most emit nothing: a success logs
    # no record at all. Derive the fields once it is known one will carry them.
    if not (latched_first_failure or terminate_others):
        return

    fields = completion_log_fields(state, idx, exit_code, ended_at)
    if latched_first_failure:
        log_completion_event(
            "pipeline_stage_first_failure",
            "pipeline stage %d exited %d, latching first failure",
            fields,
        )
    # Published before termination is requested, so a consumer learns the
    # decision even if the teardown then blocks on a stage that will not die.
    # Both conditions are spelled out rather than relying on the query alone:
    # the event reports a *newly latched* failure that leaves stages to stop,
    # which excludes a later failure, a final-stage failure, and a
    # single-stage pipeline.
    if latched_first_failure and terminate_others:
        emit_fail_fast_event(state.observation(idx), fields)
    if terminate_others:
        await _terminate_and_report(state, processes, cancel_grace, fields)


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
    stages: _StageWaitContext,
) -> _PipelineWaitResult:
    """Wait for pipeline completion, ensuring subprocess cleanup on cancellation."""
    state = _PipelineWaitState.from_processes(processes, stages=stages)

    caught: BaseException | None = None
    pipe_results: list[object] | None = None
    try:
        pending = set(state.wait_tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )

            # `asyncio.wait` returns an unordered set, so stages that land in
            # the same batch have no completion order left to observe; taking
            # them as they fall out of the set would make `failure_index`
            # depend on set iteration. Break the tie by stage index: in a
            # pipeline an upstream failure is what causes the downstream ones
            # it triggers, so the earliest stage is the one worth reporting.
            for wait_task in sorted(done, key=lambda task: state.task_to_index[task]):
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
