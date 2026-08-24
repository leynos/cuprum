"""What a fail-fast pipeline teardown tells the outside world.

`cuprum._pipeline_wait` decides *which* completion fails the pipeline fast;
this module owns the structured warning records and the sanitized
``pipeline_fail_fast`` :class:`~cuprum.events.ExecEvent` payload. Keeping those
reports apart from the ordering transition makes the published contract harder
to change accidentally.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ

from cuprum._pipeline_types import _EventDetails

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation
    from cuprum._pipeline_wait import _PipelineWaitState


_LOGGER = logging.getLogger("cuprum._pipeline_wait")


@dc.dataclass(frozen=True, slots=True)
class _CompletionLogFields:
    """Typed payload shared by the pipeline fail-fast event."""

    stage_index: int
    stage_count: int
    exit_code: int
    duration_s: float
    exec_id: str | None


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

    ``stage_index`` alone does not say which pipeline a record came from, so
    the stage's execution token travels with it: concurrent pipelines each
    report their own stage 0, and the token is what joins a record to the span
    and lifecycle events the observe hooks publish for that same stage.

    Returns
    -------
    _CompletionLogFields
        The structured fields for the completed stage.
    """
    return _CompletionLogFields(
        stage_index=completed_idx,
        stage_count=len(state.exit_codes),
        exit_code=exit_code,
        duration_s=max(0.0, ended_at - state.started_at[completed_idx]),
        exec_id=_observation_exec_id(state.observation(completed_idx)),
    )


def _observation_exec_id(observation: _StageObservation | None) -> str | None:
    """Render a stage token for a record without a per-pipeline UUID tuple."""
    return None if observation is None else str(observation.exec_id)


def _log_completion_event(
    action: str,
    message: str,
    fields: _CompletionLogFields,
    **extra_fields: object,
) -> None:
    """Emit one structured pipeline-wait WARNING with stable ``cuprum_`` keys."""
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


def _emit_fail_fast_event(
    observation: _StageObservation | None,
    fields: _CompletionLogFields,
) -> None:
    """Publish the fail-fast decision to the observe hooks as one ``ExecEvent``.

    The event repeats the failing stage's own ``exec_id``, so a tracing adapter
    can attach it to the span that stage's ``start`` event already opened, and
    reports the stage's position, the pipeline width, the exit code, and how
    long the stage ran. Unlike lifecycle events, its argv, cwd, environment,
    and caller tags are deliberately absent so this decision telemetry cannot
    disclose command-line secrets.

    ``observation`` is ``None`` when the wait state was built without
    observation context, as the transition-level tests do; there is then no
    hook set to publish to.

    A hook that raises propagates out of the pipeline's wait, which fails the
    run before termination is requested. Every still-running stage is torn down
    regardless, by ``_cleanup_pipeline_on_error`` on the way out, so a broken
    hook costs the fail-fast *report* rather than leaking processes.
    """
    if observation is None:
        return
    observation.emit_fail_fast(
        _EventDetails(
            pid=None,
            exit_code=fields.exit_code,
            duration_s=fields.duration_s,
            stage_index=fields.stage_index,
            stage_count=fields.stage_count,
        ),
    )
