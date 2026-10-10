"""The liveness policy that bounds a repeated native pipeline hand-off.

This module holds the clock arithmetic alone, so it can be read and tested
without a pipeline. The runner that applies it and the stall model that
classifies its result live in :mod:`tests.behaviour._native_pipeline_hand_off`
and :mod:`tests.behaviour._native_pipeline_stall`; the split keeps all three
modules inside the Pylint module-length limit.
"""

from __future__ import annotations

import dataclasses as dc
import time
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: How long the observation stream may stay quiet before an attempt is a stall.
#: A healthy attempt emits at least a ``start`` per stage and an ``exit`` per
#: stage, and takes about 0.02 s end to end, so this is more than an order of
#: magnitude of headroom over the healthy case.
NO_PROGRESS_INTERVAL_S = 0.5
#: How often the monitor samples the progress clock while progress continues.
PROGRESS_PROBE_INTERVAL_S = 0.05

#: The stable Cuprum tag carrying a pipeline event's zero-based stage position.
STAGE_INDEX_TAG = "pipeline_stage_index"
#: The phases that prove a pipeline is still making observable progress.
PROGRESS_PHASES = frozenset({"start", "stdout", "stderr", "exit"})


@dc.dataclass(frozen=True, slots=True)
class LivenessProgress:
    """Where the progress clock stood when a stall was captured.

    The liveness policy is otherwise unobservable from outside the runner: the
    stall snapshot says what the children were doing, and these two numbers say
    how long the observation stream had been quiet and how much of the
    suite-safety backstop had been consumed.
    """

    quiet_for_s: float
    deadline_remaining_s: float


def quiet_for_s(
    last_progress_at: float,
    *,
    monotonic_clock: cabc.Callable[[], float] = time.monotonic,
) -> float:
    """Return how long the observation stream has been quiet.

    The clock is a dependency rather than a module global so a caller can drive
    the arithmetic with no wall-clock wait; the default is the real clock, so
    callers that do not care read exactly as they did before.

    Parameters
    ----------
    last_progress_at : float
        The monotonic instant at which progress was last observed.
    monotonic_clock : Callable[[], float], default=time.monotonic
        The clock supplying the current instant.

    Returns
    -------
    float
        Seconds elapsed since the last observable lifecycle event.
    """
    return monotonic_clock() - last_progress_at


@dc.dataclass(frozen=True, slots=True)
class LivenessBound:
    """The liveness policy the runner applies, as one value.

    It carries the whole policy — how long the observation stream may stay
    quiet and how much backstop the whole attempt loop has between them —
    rather than a deadline alone, so a caller can neither bound a run without
    bounding an attempt nor substitute one bound for the other.
    """

    no_progress_interval_s: float = NO_PROGRESS_INTERVAL_S
    aggregate_deadline: float | None = None
    monotonic_clock: cabc.Callable[[], float] = time.monotonic

    def _quiet_for_s(self, last_progress_at: float) -> float:
        """Return the quiet period, read through this bound's clock."""
        return quiet_for_s(last_progress_at, monotonic_clock=self.monotonic_clock)

    def _remaining_s(self) -> float:
        """Return how much of the suite-safety backstop is left."""
        if self.aggregate_deadline is None:
            return float("inf")
        return max(0.0, self.aggregate_deadline - self.monotonic_clock())

    def progress(self, last_progress_at: float) -> LivenessProgress:
        """Describe where the progress clock stood when a stall was captured.

        Returns
        -------
        LivenessProgress
            The quiet period and the remaining backstop, sampled together.
        """
        return LivenessProgress(
            self._quiet_for_s(last_progress_at),
            self._remaining_s(),
        )

    def settled(self, last_progress_at: float) -> bool:
        """Report whether the observation stream has gone quiet for too long.

        Returns
        -------
        bool
            ``True`` when no lifecycle event has arrived for the interval.
        """
        return self._quiet_for_s(last_progress_at) >= self.no_progress_interval_s

    def expired(self, last_progress_at: float) -> bool:
        """Report whether the suite-safety backstop has been reached.

        The quiet period is tested first and wins ties, which is what makes the
        backstop a backstop: a run that has already gone quiet is classified
        from its evidence rather than from the clock.

        Returns
        -------
        bool
            ``True`` when the backstop expired while the run was still
            emitting progress.
        """
        return self._expired_after(self._quiet_for_s(last_progress_at))

    def pre_attempt_backstop_reached(self) -> bool:
        """Report whether the backstop expired before an attempt could start.

        This is deliberately a plain deadline comparison rather than
        :meth:`expired`. That method's tie-break asks "has the observation
        stream gone quiet?", which can only be answered once an attempt has a
        progress clock; before an attempt starts there is no stream to be
        quiet, so the backstop is the only thing that can have expired. It is
        also why this path skips rather than reporting: no child state exists
        yet for the classifier to read.

        Returns
        -------
        bool
            ``True`` when the suite-safety backstop is spent.
        """
        if self.aggregate_deadline is None:
            return False
        return self.monotonic_clock() >= self.aggregate_deadline

    def _expired_after(self, quiet_s: float) -> bool:
        """Report whether the backstop has been reached, given a quiet period."""
        if quiet_s >= self.no_progress_interval_s:
            return False
        return self.monotonic_clock() >= (self.aggregate_deadline or float("inf"))

    def pause_s(self, last_progress_at: float) -> float:
        """Return how long to wait before sampling progress again.

        Returns
        -------
        float
            A non-negative delay bounded by the probe interval, the remaining
            quiet period, and the remaining backstop.
        """
        return max(
            0.0,
            min(
                PROGRESS_PROBE_INTERVAL_S,
                self.no_progress_interval_s - self._quiet_for_s(last_progress_at),
                self._remaining_s(),
            ),
        )
