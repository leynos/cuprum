"""Presentation-sink session lifecycle for pipeline runs.

Split from ``cuprum._pipeline_internals`` so that module stays about *running*
a pipeline: this module owns the pipeline half of the presentation-sink
contract — mapping a terminal event onto the bounded ``SessionOutcome`` set,
and closing the session the adapter opened. The single-command counterparts of
these helpers live in ``cuprum.sh``.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._pipeline_collect import _sh_module
from cuprum.sinks import base as sinks

if typ.TYPE_CHECKING:
    from cuprum.sh import CommandResult


def _pipeline_result_outcome(
    stage_results: list[CommandResult],
) -> sinks.SessionOutcome:
    """Map a completed pipeline's stage results onto the terminal-outcome set.

    A pipeline fails when any stage failed; the annotation reports the first
    failing stage's exit code. A zero exit code for every stage is success.

    Returns
    -------
    sinks.SessionOutcome
        The terminal report for the pipeline.
    """
    failed = next((r for r in stage_results if r.exit_code != 0), None)
    if failed is None:
        return sinks.SessionOutcome(
            outcome=sinks.TerminalOutcome.EXIT_ZERO,
            exit_code=0,
        )
    return sinks.SessionOutcome(
        outcome=sinks.TerminalOutcome.EXIT_NONZERO,
        exit_code=failed.exit_code,
    )


def _pipeline_error_outcome(error: BaseException) -> sinks.SessionOutcome:
    """Map a pipeline's terminal error onto the terminal-outcome set.

    Mirrors the single-command mapping: only bounded categorical categories
    reach the adapter, never exception text or argv.

    Returns
    -------
    sinks.SessionOutcome
        The terminal report for the pipeline.
    """
    timeout_expired = _sh_module().TimeoutExpired
    match error:
        case timeout_expired():
            return sinks.SessionOutcome(
                outcome=sinks.TerminalOutcome.TIMEOUT,
                detail="timeout",
            )
        case asyncio.CancelledError():
            return sinks.SessionOutcome(outcome=sinks.TerminalOutcome.CANCELLED)
        case _:
            return sinks.SessionOutcome(outcome=sinks.TerminalOutcome.ERROR)


def _close_pipeline_sink_session(
    session: sinks.OutputSession | None,
    *,
    outcome: sinks.SessionOutcome,
) -> None:
    """Finalize the pipeline's presentation-sink session on any terminal path.

    The adapter's ``close`` is idempotent by protocol, so overlapping
    terminal paths cannot double-annotate. Called synchronously on each
    terminal path before the pending-task drains: the close is non-blocking
    (a buffered write), and the drain that follows is the shielded step.

    Parameters
    ----------
    session : sinks.OutputSession | None
        The pipeline's active session, or ``None`` when no sink was given.
    outcome : sinks.SessionOutcome
        The terminal report for the pipeline.
    """
    if session is None:
        return
    session.close(outcome)


__all__ = [
    "_close_pipeline_sink_session",
    "_pipeline_error_outcome",
    "_pipeline_result_outcome",
]
