"""Observe-event tests for the subprocess timeout paths.

Covers the ``timeout`` and ``teardown_error`` ``ExecEvent`` phases emitted on
expiry and on a teardown drain failure, including the guarantee that a failing
observe hook cannot mask the timeout it describes. The structured-logging side
of the same contract lives in ``test_subprocess_timeout_logging``.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum._subprocess_wait import (
    _drain_stream_consumers,
    _wait_for_exit_code_within_timeout,
)
from cuprum.sh import ExecutionContext
from cuprum.unittests._timeout_test_helpers import (
    _DeadlineExecution,
    _ExitedProcess,
    _RaisingObservation,
    _RecordingObservation,
    _TimeoutWaitProcess,
)

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _EventDetails, _StageObservation
    from cuprum._subprocess_execution import _SubprocessExecution


def _assert_timeout_event_fields(
    details: _EventDetails,
    *,
    mode: str,
    pid: int,
    timeout_s: float,
) -> None:
    """Assert the stable fields on a ``timeout`` observe event."""
    expected: dict[str, object] = {
        "timeout_mode": mode,
        "operation": "wait",
        "pid": pid,
        "timeout_s": timeout_s,
        "error_type": "TimeoutError",
    }
    for key, want in expected.items():
        assert getattr(details, key) == want, (
            f"the timeout observe event must carry {key}={want!r}, "
            f"got {getattr(details, key)!r}"
        )


def _single_timeout_event(
    observation: _RecordingObservation,
) -> _EventDetails:
    """Return the sole ``timeout`` event's details, asserting uniqueness."""
    timeouts = [details for phase, details in observation.events if phase == "timeout"]
    assert len(timeouts) == 1, (
        f"expected exactly one 'timeout' observe event, got {observation.events!r}"
    )
    return timeouts[0]


def test_elapsed_timeout_emits_observe_event() -> None:
    """An elapsed deadline emits a ``timeout`` observe event with stable fields."""

    async def run_case() -> _RecordingObservation:
        """Let a positive deadline elapse and return the recorded observation."""
        process = _TimeoutWaitProcess()
        recorder = _RecordingObservation()
        execution = _DeadlineExecution(
            ctx=ExecutionContext(cancel_grace=0.1),
            timeout=0.05,
            observation=recorder,
        )

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )
        return recorder

    observation = asyncio.run(run_case())
    details = _single_timeout_event(observation)
    _assert_timeout_event_fields(
        details, mode="elapsed_deadline", pid=4321, timeout_s=0.05
    )


def test_non_positive_timeout_emits_observe_event() -> None:
    """The immediate fast path emits a ``timeout`` event tagged non-positive."""

    async def run_case() -> _RecordingObservation:
        """Trigger the immediate fast path and return the recorded observation."""
        process = _ExitedProcess()
        recorder = _RecordingObservation()
        execution = _DeadlineExecution(
            ctx=ExecutionContext(), timeout=0, observation=recorder
        )

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )
        return recorder

    observation = asyncio.run(run_case())
    details = _single_timeout_event(observation)
    _assert_timeout_event_fields(
        details, mode="non_positive_immediate", pid=5678, timeout_s=0
    )


def test_teardown_drain_failure_emits_observe_event() -> None:
    """A consumer drain failure emits a ``teardown_error`` observe event."""

    async def failing_consumer() -> str | None:
        """Block until cancelled, then fail during cleanup instead of cancelling."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            msg = "consumer boom"
            raise ValueError(msg) from None
        return None

    async def run_case() -> _RecordingObservation:
        """Drain a failing consumer through the shared drain."""
        consumer = asyncio.create_task(failing_consumer())
        completed = asyncio.create_task(asyncio.sleep(0, result="stderr"))
        observation = _RecordingObservation()
        await asyncio.sleep(0)

        await _drain_stream_consumers(
            (consumer, completed),
            capture=False,
            pid=5678,
            observation=typ.cast("_StageObservation", observation),
        )
        return observation

    observation = asyncio.run(run_case())
    teardowns = [
        details for phase, details in observation.events if phase == "teardown_error"
    ]
    assert len(teardowns) == 1, (
        f"expected one 'teardown_error' event, got {observation.events!r}"
    )
    details = teardowns[0]
    assert details.operation == "drain", (
        f"the teardown_error event must carry operation='drain', "
        f"got {details.operation!r}"
    )
    assert details.pid == 5678, (
        f"the teardown_error event must carry pid=5678, got {details.pid!r}"
    )
    assert details.error_type is not None, (
        "the teardown_error event must carry the offending error class"
    )
    assert "ValueError" in details.error_type, (
        f"the teardown_error event must name ValueError as the drain failure, "
        f"got {details.error_type!r}"
    )


def test_observe_emit_failure_does_not_mask_timeout() -> None:
    """A raising observe hook cannot replace the ``TimeoutError`` on expiry.

    The immediate fast path emits its ``timeout`` event best-effort; when the
    observation's ``emit`` raises, that failure is swallowed and ``TimeoutError``
    still propagates.
    """

    async def run_case() -> None:
        """Expire immediately with an observation whose emit always raises."""
        process = _ExitedProcess()
        execution = _DeadlineExecution(
            ctx=ExecutionContext(),
            timeout=0,
            observation=_RaisingObservation(),
        )

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )

    asyncio.run(run_case())
