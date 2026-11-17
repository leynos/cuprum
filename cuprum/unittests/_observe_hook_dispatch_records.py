"""The log records observe-hook dispatch must emit.

This module states, record by record, what dispatch's successful scheduling, its
synchronous hook failures, and each scheduled task's lifecycle must look like.
A comparison covers each full record — level, message, structured fields and
``exc_info`` — so drift in wording or in a structured field is reported rather
than hidden behind a message prefix.

The recorded messages are a specification, not a copy of the implementation:
they are what the released behaviour promises, and a property run fails if the
dispatcher's wording or structured fields drift from them.

The expectation those records are built from, and the hooks that produce it,
live in :mod:`_observe_hook_dispatch_oracle`.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import typing as typ

from cuprum.unittests._observe_hook_dispatch_oracle import (
    DispatchExpectation,
    scheduled_probe_indices,
)
from cuprum.unittests._observe_hook_dispatch_shapes import TASK_FAILURE_KINDS

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent

_LOGGER_NAME = "cuprum._observability"

# Message prefixes the dispatcher owns. A comparison considers only records
# that carry one of these, so unrelated logging cannot make it pass or fail.
DISPATCH_MESSAGE_PREFIXES: tuple[str, ...] = (
    "observe_hook_task_scheduled",
    "observe_hook_failed",
    "observe_hook_task_started",
    "observe_hook_task_finished",
    "observe_hook_task_failed",
)

# The records emitted from inside a scheduled task. They are compared without
# regard to order because a suspension inside an awaitable interleaves them.
_LIFECYCLE_PREFIXES: tuple[str, ...] = (
    "observe_hook_task_started",
    "observe_hook_task_finished",
    "observe_hook_task_failed",
)


@dc.dataclass(frozen=True, slots=True)
class ExpectedRecord:
    """One log record the dispatcher must emit.

    Attributes
    ----------
    level:
        The record's numeric level.
    message:
        The fully formatted message, as ``LogRecord.getMessage()`` returns it.
    extra:
        The structured fields the record must carry, with their values.
    exc_type:
        The type the record's ``exc_info`` must name, or ``None`` for no
        exception information.

    """

    level: int
    message: str
    extra: dict[str, object]
    exc_type: type[BaseException] | None


def _scheduling_records(
    expectation: DispatchExpectation,
    event: ExecEvent,
) -> list[ExpectedRecord]:
    """State the scheduling and failure records in the order hooks run.

    Each record is emitted where its hook sits in the call order, not after the
    others: a settled dispatch keeps calling hooks past a failure, so a failure
    record can precede later scheduling records. Its task count is the number
    scheduled *so far*, which is what the dispatcher has to report. Cancellation
    is not a failure and is deliberately not reported.

    Returns
    -------
    list[ExpectedRecord]
        The scheduling and failure records, in emission order.
    """
    phase = event.phase
    program = str(event.program)
    scheduled_at = set(scheduled_probe_indices(expectation))
    failures_at = {
        failure.index: failure.error
        for failure in expectation.failures
        if not issubclass(type(failure.error), asyncio.CancelledError)
    }
    records: list[ExpectedRecord] = []
    count = 0
    for index in range(len(expectation.called)):
        if index in scheduled_at:
            count += 1
            records.append(
                ExpectedRecord(
                    level=logging.DEBUG,
                    message=(
                        f"observe_hook_task_scheduled phase={phase} "
                        f"program={program} count={count}"
                    ),
                    extra={
                        "cuprum_phase": phase,
                        "cuprum_program": program,
                        "cuprum_scheduled_task_count": count,
                    },
                    exc_type=None,
                )
            )
        error = failures_at.get(index)
        if error is not None:
            error_type = type(error).__name__
            records.append(
                ExpectedRecord(
                    level=logging.WARNING,
                    message=(
                        f"observe_hook_failed phase={phase} "
                        f"program={program} error={error_type}"
                    ),
                    extra={
                        "cuprum_phase": phase,
                        "cuprum_program": program,
                        "cuprum_error_type": error_type,
                        "cuprum_scheduled_task_count": count,
                    },
                    exc_type=type(error),
                )
            )
    return records


def _lifecycle_records(
    expectation: DispatchExpectation,
    event: ExecEvent,
) -> list[ExpectedRecord]:
    """State each scheduled task's lifecycle, including any awaited failure."""
    records: list[ExpectedRecord] = []
    for kind in expectation.scheduled:
        records.append(
            ExpectedRecord(
                level=logging.DEBUG,
                message=f"observe_hook_task_started phase={event.phase}",
                extra={"cuprum_phase": event.phase},
                exc_type=None,
            )
        )
        failure_type = TASK_FAILURE_KINDS.get(kind)
        if failure_type is not None:
            # ``Logger.exception`` reports at ERROR, not WARNING.
            records.append(
                ExpectedRecord(
                    level=logging.ERROR,
                    message=(
                        f"observe_hook_task_failed phase={event.phase} "
                        f"error={failure_type.__name__}"
                    ),
                    extra={
                        "cuprum_phase": event.phase,
                        "cuprum_error_type": failure_type.__name__,
                    },
                    exc_type=failure_type,
                )
            )
            continue
        records.append(
            ExpectedRecord(
                level=logging.DEBUG,
                message=f"observe_hook_task_finished phase={event.phase}",
                extra={"cuprum_phase": event.phase},
                exc_type=None,
            )
        )
    return records


def expected_records(
    expectation: DispatchExpectation,
    event: ExecEvent,
) -> tuple[list[ExpectedRecord], list[ExpectedRecord]]:
    """State the log records the dispatcher must emit for ``expectation``.

    Returns
    -------
    tuple[list[ExpectedRecord], list[ExpectedRecord]]
        The scheduling and failure records in emission order, then the task
        lifecycle records, which are compared without regard to order because
        a suspension inside an awaitable may interleave them.
    """
    return (
        _scheduling_records(expectation, event),
        _lifecycle_records(expectation, event),
    )
