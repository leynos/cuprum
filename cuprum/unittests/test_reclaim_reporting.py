"""What a suppressed reclaim failure must still tell an operator.

The reclaim suppresses an ordinary cleanup failure so the error the caller is
already unwinding reaches it unchanged and the steps after the failed one still
run. That suppression is only safe if the failure is still visible, so each one
is reported on two channels: a structured ``cuprum.reclaim`` log record and a
``teardown_error`` observe event. Both name the step that failed, which is what
separates a child that could not be terminated from a descriptor that could not
be released.

Reporting is best-effort. A misconfigured log handler must not replace the
error being described, so that case is exercised here too.
"""

from __future__ import annotations

import logging
import types
import typing as typ

import pytest

from cuprum._pipeline_types import _ExecutionHooks
from cuprum._reclaim_reporting import _report_reclaim_failure
from cuprum.capture_policy import FileBackedCapture
from cuprum.unittests._adapter_test_support import capturing_logger
from cuprum.unittests._startup_abandon_support import _execution

if typ.TYPE_CHECKING:
    import asyncio
    import pathlib

    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.events import ExecEvent

#: The pid the fake child reports. Any value works: the record only has to
#: carry the pid the run already announced, so a case can correlate the two.
_FAKE_PID = 4242


class _RaisingHandler(logging.Handler):
    """A handler that fails, as a misconfigured observability sink would."""

    def emit(self, record: logging.LogRecord) -> None:
        """Fail instead of recording *record*."""
        msg = "the sink is misconfigured"
        raise RuntimeError(msg)


def _fake_process() -> asyncio.subprocess.Process:
    """Return the minimum a reclaim report reads from a child."""
    return typ.cast(
        "asyncio.subprocess.Process",
        types.SimpleNamespace(pid=_FAKE_PID),
    )


def _observing_execution(
    tmp_path: pathlib.Path,
    events: list[ExecEvent],
) -> _SubprocessExecution:
    """Build a run bundle whose observation records every emitted event."""
    execution = _execution(
        FileBackedCapture(spill_threshold_bytes=None, directory=tmp_path),
    )
    execution.observation.hooks = _ExecutionHooks(
        before_hooks=(),
        after_hooks=(),
        observe_hooks=(events.append,),
    )
    return execution


def test_reclaim_failure_is_logged_with_its_operation(
    tmp_path: pathlib.Path,
) -> None:
    """The log record names the execution, the child, and the failed step."""
    execution = _observing_execution(tmp_path, [])

    with capturing_logger("cuprum.reclaim") as capture:
        _report_reclaim_failure(
            _fake_process(),
            execution,
            operation="terminate",
            error=OSError("cannot signal"),
        )

    assert len(capture.records) == 1, (
        f"one suppressed failure must produce one record, got {capture.records!r}"
    )
    record = capture.records[0]
    assert record.levelno == logging.WARNING, (
        "a reclaim that could not finish a step is a warning, not routine noise"
    )
    # The structured fields are extras, which ``LogRecord`` does not declare,
    # so they are read off the record's own attribute mapping.
    fields = vars(record)
    assert fields["cuprum_exec_id"] == str(execution.observation.exec_id), (
        "the record must correlate with the execution the run announced"
    )
    assert fields["cuprum_pid"] == _FAKE_PID, (
        "the record must name the child the reclaim was releasing"
    )
    assert fields["cuprum_operation"] == "terminate", (
        "the record must attribute the failure to the step that raised"
    )
    assert fields["cuprum_error_type"] == "OSError", (
        "only the class name is recorded, so no message payload reaches a log"
    )


def test_reclaim_failure_emits_a_teardown_error_event(
    tmp_path: pathlib.Path,
) -> None:
    """The observe event carries the same bounded operation as the log."""
    events: list[ExecEvent] = []
    execution = _observing_execution(tmp_path, events)

    _report_reclaim_failure(
        _fake_process(),
        execution,
        operation="capture_cleanup",
        error=OSError("cannot remove"),
    )

    assert len(events) == 1, f"expected one observe event, got {events!r}"
    event = events[0]
    assert event.phase == "teardown_error", (
        "the reclaim reports through the teardown phase, not a start or exit one"
    )
    assert event.pid == _FAKE_PID, (
        "the event must name the child the reclaim was releasing"
    )
    assert event.operation == "capture_cleanup", (
        "the event must carry the step that failed"
    )
    assert event.error_type == "OSError", (
        "only the class name is recorded, so no message payload reaches a sink"
    )
    assert event.exec_id == execution.observation.exec_id, (
        "the event must correlate with the execution the run announced"
    )


def test_a_failing_log_handler_does_not_displace_the_reported_error(
    tmp_path: pathlib.Path,
) -> None:
    """Telemetry is best-effort: a broken sink must not raise from the reclaim.

    The caller is already unwinding the error this report describes. If a
    handler could raise through the report, that error would be replaced by a
    logging failure and the remaining reclaim steps would be skipped.
    """
    execution = _observing_execution(tmp_path, [])
    logger = logging.getLogger("cuprum.reclaim")
    handler = _RaisingHandler()
    logger.addHandler(handler)
    try:
        _report_reclaim_failure(
            _fake_process(),
            execution,
            operation="stdin_close",
            error=OSError("cannot close"),
        )
    finally:
        logger.removeHandler(handler)


@pytest.mark.parametrize(
    "operation",
    ["terminate", "task_settle", "stdin_close", "output_discard", "transport_close"],
)
def test_every_reclaim_operation_is_reported_verbatim(
    tmp_path: pathlib.Path,
    operation: str,
) -> None:
    """Each step's category reaches both channels unchanged.

    The categories are the bounded label set of
    ``cuprum_teardown_errors_total``, so a case that renamed or dropped one
    would silently merge two different faults into one series.
    """
    events: list[ExecEvent] = []
    execution = _observing_execution(tmp_path, events)

    with capturing_logger("cuprum.reclaim") as capture:
        _report_reclaim_failure(
            _fake_process(),
            execution,
            operation=typ.cast("typ.Any", operation),
            error=OSError("failed"),
        )

    assert vars(capture.records[0])["cuprum_operation"] == operation, (
        f"the log must name {operation!r}, not a neighbouring category"
    )
    assert events[0].operation == operation, (
        f"the observe event must name {operation!r}, not a neighbouring category"
    )
