"""How a stopped command is settled, and what the stop is reported as.

Stopping and settling are two steps because a cancellation arriving during the
first must not skip the second. ``_shielded_cleanup`` finishes the teardown and
then re-raises the caller's cancellation, so awaiting a stop and settling on the
following line leaves a handle whose child was reaped but whose result stays
``None`` forever — reaping without settling is the half-owned state the handle
exists to prevent, and every later ``wait``, ``terminate``, or ``kill`` would
report it.

Both the public stop operations and :meth:`RunningCommand.__aexit__` need that
ordering, so the rule is stated once here rather than repeated on each of them.
The functions take the stop as an awaitable and the settle as a factory rather
than a handle, so the weighing can be exercised without constructing a run.
"""

from __future__ import annotations

import typing as typ

from cuprum._process_lifecycle import (
    _run_shielded_step,
    _ShieldedStepFailure,
    _TerminationOutcome,
)
from cuprum.control_events import ControlOutcome
from cuprum.control_observation import _complete_control

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.control_observation import _ControlMeasurement
    from cuprum.sh import CommandResult

__all__ = [
    "_settle_after_stop",
    "_settle_teardown",
    "_stop_outcome_from",
]

type _StopRecord = tuple[_ControlMeasurement | None, _TerminationOutcome]
"""A stop's control measurement paired with the outcome it produced.

The measurement is ``None`` for the unobserved case, where ``_start_control``
allocated nothing because no hook had opted in.
"""


def _stop_outcome_from(measured: _StopRecord | None) -> ControlOutcome:
    """Classify what a stop did to the child, when it produced a measurement.

    ``None`` means the stop never reached its measurement — the step was
    cancelled or failed before it began. No delivery is then known to have
    happened, and the alternative readings are worse: claiming ``delivered``
    would invent a signal the handle never saw sent, and claiming ``escalated``
    would invent a grace window it never ran out of. The caller is at that
    moment raising its own cancellation rather than reading this, so the
    classification only has to be honest for an observer, not actionable.

    Refusal is read from the outcome rather than inferred from a missing
    delivery: a stop that reached a child the platforms's grace path cannot
    start on, and one that found the child already gone, both deliver nothing,
    and the two are different facts for whoever is watching.

    Returns
    -------
    ControlOutcome
        The closed outcome naming what the stop did, or what is known about it
        when no measurement was produced.
    """
    if measured is None:
        return ControlOutcome.REFUSED_EXITED
    _, outcome = measured
    if outcome.escalated:
        return ControlOutcome.ESCALATED
    if outcome.delivered:
        return ControlOutcome.DELIVERED
    if outcome.refused:
        return ControlOutcome.REFUSED_UNSUPPORTED
    return ControlOutcome.REFUSED_EXITED


async def _settle_after_stop(
    stop: cabc.Awaitable[_StopRecord],
    settle: cabc.Callable[[], cabc.Awaitable[CommandResult]],
) -> CommandResult:
    """Run a public stop and settle the run however that stop ended.

    The stop's control measurement is completed before the settle is awaited,
    and in every case the stop completed one: a stop that was cancelled after
    it delivered still stopped the child, and the event has to say so. Holding
    the completion until the settle returned would lose it whenever the settle
    raised — a deadline that expired, a cancellation, a teardown failure —
    which is exactly when an operator most needs to know a stop happened.

    The stop's failure outranks the settle's, because the stop is what the
    caller asked for while the settle is what this call does on its way out. A
    cancellation outranks both, so a cancelled caller sees its own cancellation
    rather than a teardown error it never asked about.

    The settle runs even when the stop was cancelled or failed, because reaping
    without settling is the half-owned state the handle exists to prevent.

    Returns
    -------
    CommandResult
        The outcome the settle recorded once the child was reaped.
    """
    stop_step = await _run_shielded_step(stop)
    if stop_step.value is not None:
        measurement, _outcome = stop_step.value
        _complete_control(measurement, _stop_outcome_from(stop_step.value))
    settle_step = await _run_shielded_step(settle())
    return await _weigh(stop_step, settle_step, settle)


async def _settle_teardown[T](
    stop: cabc.Awaitable[T],
    settle: cabc.Callable[[], cabc.Awaitable[CommandResult]],
    exc: BaseException | None,
) -> None:
    """Settle the child on every exit from an ``async with`` block.

    Never suppresses: the block's own exception reaches the caller unchanged. A
    teardown failure is reported only when the block itself succeeded —
    otherwise it would replace the error that caused the exit.

    Cancellation is the exception to that rule, and the reason is not
    stylistic. ``_shielded_cleanup`` finishes the teardown and then re-raises
    the ``CancelledError`` it absorbed, so holding it as an ordinary teardown
    failure and swallowing it because the block already failed would lose the
    cancel request: the caller handles its own error and keeps going, while the
    task that was told to stop never sees the cancellation. From 3.11 on it
    stays marked cancelled with no matching ``CancelledError``, and an
    enclosing ``asyncio.timeout`` or task-group shutdown then waits on, or
    misreports, a task that was never actually cancelled. So cancellation
    always propagates; only ordinary teardown failures defer to the block's
    error.

    The teardown drives no control measurement. It is the library's own stop
    rather than one the caller asked for, and the ``settled`` event already
    describes how the run ended; reporting it again as a control operation
    would attribute to the caller an intervention it never made.

    The stop's value is deliberately left unconstrained: this only cancels the
    step's outcome, which it never reads. Pinning it to a particular record
    would force every teardown caller to build one it does not need.
    """
    stop_step = await _run_shielded_step(stop)
    settle_step = await _run_shielded_step(settle())
    cancellation = stop_step.cancellation or settle_step.cancellation
    stop_error = (
        stop_step.failure if stop_step.failure is not None else settle_step.failure
    )
    if cancellation is not None:
        raise cancellation
    if stop_error is not None and exc is None:
        raise stop_error


async def _weigh[T, U](
    stop_step: _ShieldedStepFailure[T],
    settle_step: _ShieldedStepFailure[U],
    settle: cabc.Callable[[], cabc.Awaitable[CommandResult]],
) -> CommandResult:
    """Raise the error the two steps owe the caller, or read the outcome.

    Both errors are held for the whole of both steps rather than raised as they
    arrive, because abandoning the settle would leave the half-owned handle the
    module docstring describes: a child reaped with no result recorded.

    Returns
    -------
    CommandResult
        The outcome the settle recorded. It is re-read from the settlement
        rather than taken from the step report, because a report's value may be
        absent while the caller is still owed a settled result.
    """
    cancellation = stop_step.cancellation or settle_step.cancellation
    stop_error = (
        stop_step.failure if stop_step.failure is not None else settle_step.failure
    )
    if cancellation is not None:
        raise cancellation
    if stop_error is not None:
        raise stop_error
    return await settle()
