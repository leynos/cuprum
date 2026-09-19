"""The pipeline's own result mapping onto the terminal-outcome set.

A pipeline's outcome comes from its stages rather than from a single process,
so the result it reports is pipeline-specific: the failing stage that matters
is the first one whose exit code is non-zero. The rest of the
presentation-sink session lifecycle — opening the session, closing it on every
terminal path, and mapping an error onto the bounded outcome set — is shared
with the single-command path and lives in ``cuprum._sink_lifecycle``.
"""

from __future__ import annotations

import typing as typ

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


__all__ = [
    "_pipeline_result_outcome",
]
