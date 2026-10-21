"""Delivery and idle-heartbeat setup for a pipeline run.

Split from ``cuprum._pipeline_config`` when the per-stream capture resolution
pushed that module past the 400-line ceiling. Everything here acquires or
releases the resources a pipeline run owns *besides* its stream
configurations: the shared bounded-delivery worker, the aggregate idle
heartbeat that reports through it, and the cleanup that runs when the setup
fails after some of those resources already exist.

The seam is the one the code already drew. This module decides how a run's
parent-facing output is written off the event loop; ``cuprum._pipeline_config``
decides what the run is — its resolved capture policies, echo gates, and the
stream configurations that carry them.
"""

from __future__ import annotations

import functools
import typing as typ

from cuprum._bounded_delivery import _DeliveryWorker, _new_delivery_worker
from cuprum._delivery_lifecycle import _queue_idle_diagnostic
from cuprum._idle_diagnostic import _PIPELINE_IDLE_SUBJECT
from cuprum._idle_heartbeat import _build_idle_monitor, _IdleSinkConfig
from cuprum._sink_lifecycle import _outcome_for_error, _SinkBracket
from cuprum._stream_drain_state import _RelayDiagnostics

if typ.TYPE_CHECKING:
    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum.sh import RunOutputOptions


def _build_pipeline_delivery_worker(
    output: RunOutputOptions,
    sink_bracket: _SinkBracket,
    stdout_sink: typ.IO[str],
    stderr_sink: typ.IO[str],
) -> _DeliveryWorker | None:
    """Start one shared worker for the pipeline's resolved stream sinks."""
    delivery = output.delivery
    if delivery is None:
        return None
    return _new_delivery_worker(
        delivery,
        sink_bracket.resolve_destination(stdout_sink),
        sink_bracket.resolve_destination(stderr_sink),
        operation_lock=sink_bracket.writer_lock,
    )


def _build_pipeline_idle_monitor(
    output: RunOutputOptions,
    sink_bracket: _SinkBracket,
    stderr_sink: typ.IO[str],
    delivery_worker: _DeliveryWorker | None,
) -> tuple[_IdleMonitor | None, _RelayDiagnostics | None]:
    """Build aggregate idle reporting through the pipeline's stderr route."""
    diagnostics = _RelayDiagnostics() if delivery_worker is not None else None
    idle_delivery = (
        None
        if delivery_worker is None or diagnostics is None
        else functools.partial(
            _queue_idle_diagnostic,
            delivery_worker,
            diagnostics,
        )
    )
    # Resolve through the active session so the keepalive shares the run's sink.
    idle = _build_idle_monitor(
        output.idle_after,
        output.on_idle,
        _PIPELINE_IDLE_SUBJECT,
        _IdleSinkConfig(
            sink=sink_bracket.resolve_destination(stderr_sink),
            delivery=idle_delivery,
        ),
    )
    return idle, diagnostics


def _cleanup_failed_pipeline_setup(
    output: RunOutputOptions,
    delivery_worker: _DeliveryWorker | None,
    sink_bracket: _SinkBracket,
    run_error: BaseException,
) -> None:
    """Release the delivery worker and sink acquired before setup failed."""
    if delivery_worker is not None:
        try:
            delivery_worker.close(grace=0.0)
        except BaseException as close_error:  # ruff: ignore[blind-except] - preserve the setup error.
            _note_setup_cleanup_failure(
                run_error,
                "Output delivery worker cleanup",
                close_error,
            )
    try:
        if output.delivery is None:
            sink_bracket.close(outcome=_outcome_for_error(run_error))
        else:
            sink_bracket.close_bounded(
                outcome=_outcome_for_error(run_error),
                grace=0.0,
            )
    except BaseException as close_error:  # ruff: ignore[blind-except] - preserve the setup error.
        _note_setup_cleanup_failure(
            run_error,
            "Presentation sink cleanup",
            close_error,
        )


def _note_setup_cleanup_failure(
    run_error: BaseException,
    cleanup_action: str,
    cleanup_error: BaseException,
) -> None:
    """Attach a bounded cleanup diagnostic without replacing setup failure."""
    cause = type(cleanup_error).__name__
    run_error.add_note(f"{cleanup_action} also failed ({cause}).")


__all__ = [
    "_build_pipeline_delivery_worker",
    "_build_pipeline_idle_monitor",
    "_cleanup_failed_pipeline_setup",
    "_note_setup_cleanup_failure",
]
