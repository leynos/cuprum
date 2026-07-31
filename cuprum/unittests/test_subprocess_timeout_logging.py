"""Structured-logging tests for the subprocess timeout paths.

Covers the ``cuprum.timeout`` log records emitted on expiry and on a teardown
drain failure, including the guarantee that a failing logger cannot mask the
timeout it describes. The observe-event side of the same contract lives in
``test_subprocess_timeout_observe``.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum import _subprocess_timeout
from cuprum._subprocess_execution import (
    _drain_stream_consumers,
    _wait_for_exit_code_within_timeout,
)
from cuprum.sh import ExecutionContext
from cuprum.unittests._timeout_test_helpers import (
    _DeadlineExecution,
    _ExitedProcess,
    _TimeoutWaitProcess,
)

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution


def _assert_timeout_log_fields(
    fields: dict[str, object],
    *,
    mode: str,
    pid: int,
    timeout_s: float,
) -> None:
    """Assert the stable ``cuprum_*`` fields on a timeout expiry log record."""
    expected: dict[str, object] = {
        "cuprum_timeout_mode": mode,
        "cuprum_operation": "wait",
        "cuprum_pid": pid,
        "cuprum_timeout_s": timeout_s,
        "cuprum_error_type": "TimeoutError",
    }
    for key, want in expected.items():
        assert fields.get(key) == want, (
            f"the timeout log record must carry {key}={want!r}, got {fields.get(key)!r}"
        )


_TIMEOUT_LOGGER = "cuprum.timeout"


def _single_timeout_record(
    caplog: pytest.LogCaptureFixture,
    level: int,
) -> logging.LogRecord:
    """Return the sole ``cuprum.timeout`` record at ``level``, asserting uniqueness."""
    records = [
        record
        for record in caplog.records
        if record.name == _TIMEOUT_LOGGER and record.levelno == level
    ]
    assert len(records) == 1, (
        f"expected exactly one {_TIMEOUT_LOGGER} record at level {level}, got "
        f"{[(rec.levelno, rec.getMessage()) for rec in records]}"
    )
    return records[0]


def test_ordinary_timeout_expiry_logs_elapsed_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An elapsed wall-clock deadline emits a structured ``cuprum.timeout`` warning.

    The diagnostic must carry stable ``cuprum_*`` fields keyed for observability
    integrations, including ``mode="elapsed"`` to distinguish an elapsed deadline
    from an immediate non-positive expiry.
    """

    async def run_case() -> None:
        """Let a positive deadline elapse and expect a timeout."""
        process = _TimeoutWaitProcess()
        execution = _DeadlineExecution(
            ctx=ExecutionContext(cancel_grace=0.1), timeout=0.05
        )

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )

    with caplog.at_level(logging.WARNING, logger=_TIMEOUT_LOGGER):
        asyncio.run(run_case())

    fields = vars(_single_timeout_record(caplog, logging.WARNING))
    _assert_timeout_log_fields(
        fields, mode="elapsed_deadline", pid=4321, timeout_s=0.05
    )


def test_non_positive_timeout_logs_immediate_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The non-positive fast path emits a diagnostic tagged ``mode="immediate"``."""

    async def run_case() -> None:
        """Trigger the immediate fast path against an already-exited process."""
        process = _ExitedProcess()
        execution = _DeadlineExecution(ctx=ExecutionContext(), timeout=0)

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )

    with caplog.at_level(logging.WARNING, logger=_TIMEOUT_LOGGER):
        asyncio.run(run_case())

    fields = vars(_single_timeout_record(caplog, logging.WARNING))
    _assert_timeout_log_fields(
        fields, mode="non_positive_immediate", pid=5678, timeout_s=0
    )


def test_teardown_drain_failure_logs_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A consumer draining with an unexpected error emits a teardown diagnostic.

    The failure is absorbed to preserve the primary timeout/cancellation, but it
    must still surface through a structured ``cuprum.timeout`` error record with
    ``teardown_outcome="drain_error"`` and the offending error class.
    """

    async def failing_consumer() -> str | None:
        """Block until cancelled, then fail during cleanup instead of cancelling."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            msg = "consumer boom"
            raise ValueError(msg) from None
        return None

    async def run_case() -> None:
        """Drain a consumer that surfaces a non-cancellation error on teardown."""
        consumer = asyncio.create_task(failing_consumer())
        completed = asyncio.create_task(asyncio.sleep(0, result="stderr"))
        # Let the consumer reach its await before the drain cancels it, so its
        # cancellation handler runs and surfaces a ValueError rather than a plain
        # CancelledError during the drain.
        await asyncio.sleep(0)

        await _drain_stream_consumers((consumer, completed), pid=5678)

    with caplog.at_level(logging.ERROR, logger=_TIMEOUT_LOGGER):
        asyncio.run(run_case())

    fields = vars(_single_timeout_record(caplog, logging.ERROR))
    assert fields["cuprum_operation"] == "teardown", (
        "the drain-failure record must carry cuprum_operation='teardown', "
        f"got {fields['cuprum_operation']!r}"
    )
    assert fields["cuprum_teardown_outcome"] == "drain_error", (
        "the drain-failure record must carry "
        "cuprum_teardown_outcome='drain_error', got "
        f"{fields['cuprum_teardown_outcome']!r}"
    )
    assert fields["cuprum_pid"] == 5678, (
        f"the drain-failure record must carry cuprum_pid=5678, "
        f"got {fields['cuprum_pid']!r}"
    )
    assert "ValueError" in fields["cuprum_error_type"], (
        "the drain-failure record must name ValueError in cuprum_error_type, "
        f"got {fields['cuprum_error_type']!r}"
    )


def test_timeout_logging_failure_does_not_mask_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A telemetry failure must never replace the ``TimeoutError`` it describes.

    Even if the diagnostic logger raises, the immediate fast path must still
    propagate ``TimeoutError``; the best-effort emission swallows its own errors.
    """

    def boom(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Simulate a logging handler that raises."""
        msg = "handler exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(_subprocess_timeout._LOGGER, "log", boom)

    async def run_case() -> None:
        """Expire immediately while the logger is sabotaged to raise."""
        process = _ExitedProcess()
        execution = _DeadlineExecution(ctx=ExecutionContext(), timeout=0)

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )

    asyncio.run(run_case())
