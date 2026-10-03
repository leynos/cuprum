"""The completion-ordering transition behind a pipeline's fail-fast decision.

This is the bookkeeping ``cuprum._pipeline_wait`` waits with, kept apart from
the asyncio machinery that drives it so the ordering rules can be verified
without processes or a clock. The rule it encodes is the one that decides which
stage fails a pipeline:

- ``record_completion`` stamps a stage's exit code and injected end time, and
  latches the first non-zero exit **in completion order**. Completion order
  decides, not stage order.
- ``should_terminate_others`` reports, without mutating anything, whether that
  completion should stop every other still-running stage.

Both are pure: the clock arrives as an argument and no signal is sent from
here, which is what lets Hypothesis and CrossHair drive the transition directly
while ``cuprum._pipeline_wait`` owns the I/O around it.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._process_exit import _await_process_exit

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation, _StageWaitContext


@dc.dataclass(slots=True)
class _PipelineWaitState:
    """Mutable bookkeeping for awaiting all stages of a pipeline."""

    wait_tasks: list[asyncio.Task[int]]
    task_to_index: dict[asyncio.Task[int], int]
    exit_codes: list[int | None]
    started_at: list[float]
    ended_at: list[float | None]
    wall_clock_started_at: list[float]
    failure_index: int | None = None
    # Reporting only: the completion transition never reads this, which is why
    # it defaults to empty and the symbolic model leaves it so. Observations
    # provide the existing stage token and publish the fail-fast ``ExecEvent``.
    observations: tuple[_StageObservation, ...] = ()

    @classmethod
    def from_processes(
        cls,
        processes: list[asyncio.subprocess.Process],
        *,
        stages: _StageWaitContext,
    ) -> _PipelineWaitState:
        """Create wait state with one wait task per pipeline process."""
        wait_tasks = [
            asyncio.create_task(_await_process_exit(process)) for process in processes
        ]
        return cls(
            wait_tasks=wait_tasks,
            task_to_index={task: idx for idx, task in enumerate(wait_tasks)},
            exit_codes=[None] * len(processes),
            # Copied, not aliased: this state stamps its own bookkeeping, and
            # `stages` is meant to stay the immutable snapshot it declares.
            started_at=list(stages.started_at),
            ended_at=[None] * len(processes),
            wall_clock_started_at=list(stages.wall_clock_started_at),
            observations=stages.observations,
        )

    def observation(self, stage_index: int) -> _StageObservation | None:
        """Return a stage's observation, or ``None`` when there is none.

        Absent under the same conditions as `exec_id`, and additionally
        harmless: with no observation there is no hook set to publish the
        fail-fast event to.

        Returns
        -------
        _StageObservation | None
            The observation for ``stage_index``, or ``None`` when it is absent.
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
    ) -> bool:
        """Record a stage's completion (command).

        This is the pure completion-ordering transition behind
        [`_process_completed_task`][cuprum._pipeline_wait._process_completed_task]:
        it stamps the completed stage's exit code and end time (the clock is
        injected as ``ended_at`` so the transition is deterministic) and latches
        the *first* non-zero exit — in completion order — as ``failure_index``.

        It returns whether this completion newly requests fail-fast
        termination: only a newly latched failure from a non-final stage can
        do so. All I/O — reading the clock and terminating stages — stays
        with the caller, which separately checks whether a stage remains to
        terminate.

        Returns
        -------
        bool
            Whether this completion is a first failure on a non-final stage.

        Examples
        --------
        The first non-zero exit *in completion order* latches, even when a
        lower-indexed stage fails later::

            state = _PipelineWaitState(
                wait_tasks=[],
                task_to_index={},
                exit_codes=[None] * 3,
                started_at=[0.0] * 3, wall_clock_started_at=[0.0] * 3,
                ended_at=[None] * 3,
            )
            state.record_completion(2, 0, ended_at=1.0)
            state.record_completion(0, 1, ended_at=2.0)
            state.record_completion(1, 7, ended_at=3.0)

            assert state.failure_index == 0
            assert state.exit_codes == [1, 7, 0]
            assert state.ended_at == [2.0, 3.0, 1.0]
        """
        is_first_failure = self.failure_index is None and exit_code != 0
        self.exit_codes[completed_idx] = exit_code
        self.ended_at[completed_idx] = ended_at
        if is_first_failure:
            self.failure_index = completed_idx
        return is_first_failure and completed_idx != len(self.exit_codes) - 1

    def should_terminate_others(self, completed_idx: int) -> bool:
        """Report whether completing ``completed_idx`` should fail the pipeline fast.

        This is the query half of the transition: it inspects state without
        changing it, so it is safe to call repeatedly and in any order after
        [`record_completion`][cuprum._pipeline_wait_state._PipelineWaitState.record_completion]
        has stamped the completion.

        It answers ``True`` exactly when ``completed_idx`` is the latched first
        failure *and* is not the final stage. A failing final stage has nothing
        left to stop, so it never triggers termination. When it does answer
        ``True`` the caller terminates every *other* still-running stage — both
        upstream and downstream — not merely the ones after the failure.

        It reasons about ordering alone, so it needs no wait tasks; whether a
        sibling is still running is the caller's separate
        `_has_stages_to_terminate` check. A batch whose stages all settled
        together answers ``True`` here and still terminates nothing.

        Returns
        -------
        bool
            Whether the completion is the latched first failure and is not the
            final stage.

        Examples
        --------
        ::

            state = _PipelineWaitState(
                wait_tasks=[],
                task_to_index={},
                exit_codes=[None] * 3,
                started_at=[0.0] * 3, wall_clock_started_at=[0.0] * 3,
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
