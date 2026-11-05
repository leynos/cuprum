"""Diagnostics for a startup reclaim that could not finish a step.

The reclaim suppresses an ordinary cleanup failure so the error the caller is
already unwinding still reaches it, and so the steps after the failed one still
run. Suppressing without reporting would make a child, descriptor, task, or
spill file that survived the reclaim invisible, so each failure is recorded on
the two channels this package already uses: a structured ``cuprum.reclaim`` log
record and a ``teardown_error`` observe event.

Both channels carry the same bounded operation category, which is what lets an
operator tell a termination that failed from a descriptor that could not be
released. Every category is a literal in the reclaim module rather than a
caller-supplied string, so the ``operation`` label of
``cuprum_teardown_errors_total`` stays low-cardinality.

Every emission here is best-effort and must never displace the exception it
describes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import typing as typ

from cuprum._pipeline_types import _EventDetails
from cuprum._timeout_reporting import _safe_emit

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution

# Reclaim diagnostics ride their own logger, mirroring the ``cuprum.timeout``
# and ``cuprum.stdin`` conventions: structured ``cuprum_*`` extra fields let an
# observability integration key on stable attributes rather than parse the
# human-readable message. A separate name keeps a degraded reclaim
# distinguishable from a timeout, which is a different failure.
_LOGGER = logging.getLogger("cuprum.reclaim")

#: The reclaim steps a failure can be attributed to. Every value is a literal
#: in the reclaim module rather than a caller-supplied string, which is what
#: makes the ``operation`` label of ``cuprum_teardown_errors_total`` bounded.
type _ReclaimOperation = typ.Literal[
    "terminate",
    "task_settle",
    "stdin_close",
    "output_discard",
    "transport_close",
    "capture_cleanup",
]


def _emit_reclaim_log(
    *,
    exec_id: str,
    pid: int | None,
    operation: _ReclaimOperation,
    error_type: str,
) -> None:
    """Emit the structured log record for a suppressed reclaim failure.

    Telemetry is strictly best-effort: a misconfigured handler that raises must
    not replace the error the caller is already unwinding, nor abort the steps
    still to run. ``CancelledError`` is suppressed alongside ``Exception``
    because it derives from ``BaseException``; this body has no ``await``, so
    the reachable case is a handler that raises it.
    """
    with contextlib.suppress(Exception, asyncio.CancelledError):
        _LOGGER.warning(
            "subprocess_reclaim_failed exec_id=%s pid=%s operation=%s error=%s",
            exec_id,
            pid,
            operation,
            error_type,
            extra={
                "cuprum_exec_id": exec_id,
                "cuprum_pid": pid,
                "cuprum_operation": operation,
                "cuprum_error_type": error_type,
            },
        )


def _report_reclaim_failure(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    *,
    operation: _ReclaimOperation,
    error: BaseException,
) -> None:
    """Report one suppressed reclaim failure without displacing the caller's.

    Parameters
    ----------
    process : asyncio.subprocess.Process
        The child the reclaim was releasing; its pid correlates the record
        with the ``start`` event the run already emitted.
    execution : _SubprocessExecution
        The run's bundle, read for the observation that carries the execution
        id and receives the ``teardown_error`` event.
    operation : _ReclaimOperation
        The bounded category of the step that failed.
    error : BaseException
        The failure being suppressed. Only its class name is recorded, so a
        message carrying a path or command payload never reaches a log.
    """
    error_type = type(error).__name__
    _emit_reclaim_log(
        exec_id=str(execution.observation.exec_id),
        pid=process.pid,
        operation=operation,
        error_type=error_type,
    )
    _safe_emit(
        execution.observation,
        "teardown_error",
        _EventDetails(
            pid=process.pid,
            operation=operation,
            error_type=error_type,
        ),
    )


__all__ = ["_ReclaimOperation", "_report_reclaim_failure"]
