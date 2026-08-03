"""The structured records a fail-fast pipeline teardown emits.

`cuprum._pipeline_wait` decides *which* completion fails the pipeline fast;
this module owns what an operator is told about it. The two concerns are
separated because the record payload is a published contract — the users'
guide documents these fields — while the ordering decision is a verified
transition, and neither should be edited by accident when changing the other.

Every record carries the same core payload and is distinguished by a stable
``cuprum_action`` name, so records can be filtered by action and correlated by
``cuprum_exec_id``, the per-stage execution token the observe hooks already
publish on that stage's span and lifecycle events.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum._pipeline_wait import _PipelineWaitState

# Named for the module that owns the behaviour, not this one. The users' guide
# tells operators to attach handlers to `cuprum._pipeline_wait`, so the logger
# name is part of the published contract and must not follow `__name__` here.
_LOGGER = logging.getLogger("cuprum._pipeline_wait")


@dc.dataclass(frozen=True, slots=True)
class _CompletionLogFields:
    """Structured fields shared by the pipeline-wait completion records."""

    stage_index: int
    stage_count: int
    exit_code: int
    duration_s: float
    exec_id: str | None


def completion_log_fields(
    state: _PipelineWaitState,
    completed_idx: int,
    exit_code: int,
    ended_at: float,
) -> _CompletionLogFields:
    """Derive the log fields for a completion, including elapsed time.

    The duration comes from the injected completion time and the stage's
    recorded start, clamped at zero so a non-monotonic clock reading cannot
    report a negative elapsed time.

    ``stage_index`` alone does not say which pipeline a record came from, so
    the stage's execution token travels with it: concurrent pipelines each
    report their own stage 0, and the token is what joins a record to the span
    and lifecycle events the observe hooks publish for that same stage.
    """
    return _CompletionLogFields(
        stage_index=completed_idx,
        stage_count=len(state.exit_codes),
        exit_code=exit_code,
        duration_s=max(0.0, ended_at - state.started_at[completed_idx]),
        exec_id=state.exec_id(completed_idx),
    )


def log_completion_event(
    action: str,
    message: str,
    fields: _CompletionLogFields,
    **extra_fields: object,
) -> None:
    """Emit one structured pipeline-wait record with stable ``cuprum_`` keys.

    ``action`` is the stable event name operators filter on, so the fail-fast
    records — latching the first failure, starting termination, and finishing
    it — stay distinguishable even though they share their core payload.
    ``extra_fields`` carries the few keys that belong to one action only.
    """
    _LOGGER.warning(
        message,
        fields.stage_index,
        fields.exit_code,
        extra={
            "cuprum_action": action,
            "cuprum_stage_index": fields.stage_index,
            "cuprum_stage_count": fields.stage_count,
            "cuprum_exit_code": fields.exit_code,
            "cuprum_duration_s": fields.duration_s,
            "cuprum_exec_id": fields.exec_id,
            **extra_fields,
        },
    )
