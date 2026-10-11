"""Pipeline doubles that let a liveness test choose its own observation stream.

The native hand-off runner is bounded on *observed* progress rather than on a
wall clock, so a test trying to produce a stall would otherwise have to wait one
out and trust that the host agreed to deschedule it. The doubles here remove
that wait: they move a test-controlled clock instead, so a stall is a value the
test wrote rather than a duration it sat through, and no assertion in the suite
depends on how long anything took.

The clock each double moves is passed in rather than held here. Two modules
drive these doubles against their own tracker, and a shared module-level clock
would couple them through state neither of them asked for.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum.program import Program
from cuprum.sh import CommandResult, PipelineResult

if typ.TYPE_CHECKING:
    from cuprum.sh import Pipeline
    from cuprum.unittests._rust_pump_test_helpers import ControllableMonotonicClock


def pipeline_result(
    *,
    stdout: str | None = "HELLO",
    exit_code: int = 0,
) -> PipelineResult:
    """Build the two-stage result an attempt produces, healthy by default.

    Parameters
    ----------
    stdout : str | None
        What the stages wrote, or ``None`` for an attempt that lost it.
    exit_code : int
        The status every stage reports.

    Returns
    -------
    PipelineResult
        A two-stage result carrying ``stdout`` and ``exit_code``.
    """
    stage = CommandResult(
        program=Program("cat"),
        argv=("cat",),
        exit_code=exit_code,
        pid=1,
        stdout=stdout,
        stderr="",
    )
    return PipelineResult(stages=(stage, stage))


class CompletingPipeline:
    """A pipeline double that finishes once the runner schedules it."""

    def __init__(self, result: PipelineResult | None = None) -> None:
        """Record the result to return, healthy when none is supplied."""
        self._result = pipeline_result() if result is None else result

    async def run(self, *, timeout: float | None = None) -> PipelineResult:  # ruff: ignore[async-function-with-timeout] - mirrors the real signature
        """Complete without spawning anything, after yielding once.

        The suspension point is fidelity to the object being doubled, not a
        device for making an assertion fire. The real pipeline spawns
        subprocesses and drains their streams, so it always suspends; a double
        that computed its result in a single step would let the monitor task go
        unscheduled where the real one reliably yields to it.

        Nothing asserts on this line, and it is not what pins the completing
        path: that pin is the monitor cancellation in ``_run_liveness_bounded``.
        The line is kept anyway, and "no test needs it" is not the same as "it
        is safe to delete" — it is also what keeps this body off the
        single-statement shape the df12 ``R9104`` rule matches, and removing it
        once already produced a lint finding on this exact line.

        Returns
        -------
        PipelineResult
            The result this double was built to hand back.
        """
        await asyncio.sleep(0)
        return self._result


class StallingPipeline:
    """A pipeline double that never finishes until it is cancelled.

    ``stall_at`` is the instant the double moves ``clock`` to as its first
    action, which is what makes the monitor's next pass see a stall. The
    pipeline task is scheduled before the monitor, so the advance lands before
    the monitor's first decision; were the order reversed the monitor would
    simply sample again a probe interval later and still settle.
    """

    def __init__(
        self,
        *,
        clock: ControllableMonotonicClock,
        stall_at: float | None = None,
    ) -> None:
        """Record the clock to move and that this double has not yet run."""
        self.cancelled = False
        self._clock = clock
        self._stall_at = stall_at

    async def run(self, *, timeout: float | None = None) -> PipelineResult:  # ruff: ignore[async-function-with-timeout] - mirrors the real signature
        """Block until the runner cancels the attempt, recording that it did."""
        if self._stall_at is not None:
            self._clock.advance_to(self._stall_at)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        msg = "the stalling double must be cancelled, never completed"
        raise AssertionError(msg)


def as_pipeline(double: object) -> Pipeline:
    """Present a pipeline double at the runner's parameter type.

    The runner calls one method and the double implements it; the cast is the
    test asserting that, rather than the runner widening its parameter to a
    type that would have to describe a pipeline it never uses.

    Returns
    -------
    Pipeline
        The same object, presented at the parameter type.
    """
    return typ.cast("Pipeline", double)
