"""Projection of execution events onto the log record a consumer sees.

Split from ``cuprum.adapters.logging_adapter`` when the rebase onto ``main``
pushed that module past the 400-line ceiling. The seam is the one the adapter
already drew: everything here is a pure function from an ``ExecEvent`` to
either the record's ``extra`` mapping or its human-readable message, and none
of it holds state, opens a logger, or knows what a log level is. The adapter
module keeps the hook, the level policy, and the JSON formatter.

``cuprum.adapters.logging_adapter`` re-exports every name below. That is not
politeness: the adapter's own tests import ``_build_extra`` and
``_format_message`` from that module's path, and an internal helper's home is
not a contract those callers should have to follow.
"""

from __future__ import annotations

import typing as typ

from cuprum.adapters._support import _event_common_fields, _prefixed

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent


def _bounded_extra(
    event: ExecEvent,
    optional_fields: cabc.Iterable[tuple[str, object]],
) -> dict[str, object]:
    """Render one whitelist field set as a log record, dropping absent fields."""
    return {
        "cuprum_phase": event.phase,
        "cuprum_program": str(event.program),
        **{name: value for name, value in optional_fields if value is not None},
    }


def _correlation_fields(event: ExecEvent) -> tuple[tuple[str, object], ...]:
    """Name the bounded fields that tie one record back to its run."""
    # Every drill-down record carries this prefix: which child, which project,
    # which execution, and which operation. Grouping them here keeps the
    # per-phase builders below about their own fields, and keeps the four keys
    # from drifting apart between them.
    return (
        ("cuprum_pid", event.pid),
        ("cuprum_project", event.project),
        ("cuprum_exec_id", event.exec_id),
        ("cuprum_operation", event.operation),
    )


def _group_teardown_extra(event: ExecEvent) -> dict[str, object]:
    """Build bounded, trusted fields for a group-teardown report."""
    return _bounded_extra(
        event,
        (
            *_correlation_fields(event),
            ("cuprum_owns_group", event.owns_group),
            (
                "cuprum_group_outcome",
                None if event.group_outcome is None else str(event.group_outcome),
            ),
            ("cuprum_settle_s", event.settle_s),
        ),
    )


def _capture_eof_grace_extra(event: ExecEvent) -> dict[str, object]:
    """Build bounded, trusted fields for a capture EOF-grace expiry."""
    # ``env_mode`` and ``capture_mode`` earn their places on the same test this
    # whitelist applies to every other field: each is a closed set of values
    # resolved by production code, so neither carries caller data, and both are
    # rendered rather than passed through so the record holds the plain string
    # operators key on rather than the member's ``repr``.
    return _bounded_extra(
        event,
        (
            *_correlation_fields(event),
            ("cuprum_eof_grace_s", event.eof_grace_s),
            ("cuprum_pending_readers", event.pending_readers),
            (
                "cuprum_env_mode",
                None if event.env_mode is None else str(event.env_mode),
            ),
            (
                "cuprum_capture_mode",
                None if event.capture_mode is None else str(event.capture_mode),
            ),
        ),
    )


def _stdio_error_extra(event: ExecEvent) -> dict[str, object]:
    """Build bounded, trusted fields for a standard-stream failure diagnostic."""
    # A ``stdio_error`` can be emitted while a producer failure or a
    # cancellation is already unwinding, and it is the one record an operator
    # reads to tell a broken producer from a child that stopped reading. Only
    # bounded identifiers travel with it: the boundary category is a closed
    # set, the operation is a fixed name chosen at the call site, and
    # ``error_type`` is an exception class rather than its message. The argv
    # and the caller's tags stay out, as they do for the grace expiry.
    #
    # This set is deliberately narrower than ``_correlation_fields``: a
    # pre-spawn boundary has no child to name and a ``stdio_error`` carries no
    # project, so the shared prefix would write two fields this phase has no
    # answer for.
    return _bounded_extra(
        event,
        (
            ("cuprum_pid", event.pid),
            ("cuprum_operation", event.operation),
            ("cuprum_error_type", event.error_type),
            (
                "cuprum_error_category",
                None if event.error_category is None else str(event.error_category),
            ),
            ("cuprum_exec_id", event.exec_id),
        ),
    )


def _build_extra(event: ExecEvent) -> dict[str, object]:
    """Build structured extra data for a log record."""
    if event.phase == "capture_eof_grace_expired":
        # The grace expiry can occur after a timeout, so retain only bounded,
        # trusted correlation fields—not caller tags or the raw argument vector.
        return _capture_eof_grace_extra(event)
    if event.phase == "stdio_error":
        return _stdio_error_extra(event)

    if event.phase == "group_teardown":
        # A teardown report is reached from the same cancellation and timeout
        # paths, so it applies the same whitelist for the same reason.
        return _group_teardown_extra(event)

    extra: dict[str, object] = {"cuprum_phase": event.phase}
    common_fields = _event_common_fields(event, _prefixed("cuprum_"))
    if event.phase == "pipeline_fail_fast":
        # Fail-fast is warning-level by default. Its decision fields are enough
        # to diagnose a teardown, so do not elevate arbitrary command arguments
        # or caller tags into a channel operators commonly retain.
        extra.update(
            (name, value) for name, value in common_fields if name != "cuprum_argv"
        )
        extra["cuprum_exec_id"] = event.exec_id
        return extra

    extra.update(common_fields)
    return extra


def _format_duration(duration_s: float | None) -> str:
    """Render an elapsed time for a log message, or ``unknown`` when absent."""
    return "unknown" if duration_s is None else f"{duration_s:.6f}"


def _format_exit_message(event: ExecEvent) -> str:
    """Render the message for a completed subprocess."""
    return (
        f"cuprum.exit program={event.program} pid={event.pid} "
        f"exit_code={event.exit_code} "
        f"duration_s={_format_duration(event.duration_s)}"
    )


def _format_fail_fast_message(event: ExecEvent) -> str:
    """Render the message for a pipeline's fail-fast decision.

    Rendered explicitly because the generic unhandled-phase message reports
    only the program, which is the one part of this event that says nothing:
    the point of the record is which stage of how many failed, and how.

    Returns
    -------
    str
        Human-readable fail-fast message containing the decision fields.
    """
    return (
        f"cuprum.pipeline_fail_fast program={event.program} "
        f"stage_index={event.stage_index} "
        f"stage_count={event.stage_count} "
        f"exit_code={event.exit_code} "
        f"duration_s={_format_duration(event.duration_s)}"
    )


def _format_message(event: ExecEvent) -> str:
    """Format a human-readable log message for the event."""
    program = event.program
    match event.phase:
        case "plan":
            message = f"cuprum.plan program={program} argv={event.argv!r}"
        case "start":
            message = f"cuprum.start program={program} pid={event.pid}"
        case "stdout" | "stderr":
            # One rendering for both: they differ only in the phase name.
            message = f"cuprum.{event.phase} pid={event.pid} line={event.line!r}"
        case "exit":
            message = _format_exit_message(event)
        case "settled":
            message = (
                f"cuprum.settled program={program} "
                f"terminal_outcome={event.terminal_outcome}"
            )
        case "pipeline_fail_fast":
            message = _format_fail_fast_message(event)
        case _:
            message = f"cuprum.{event.phase} program={program}"
    return message


__all__ = [
    "_build_extra",
    "_format_message",
]
