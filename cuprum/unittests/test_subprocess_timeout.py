"""Unit tests for subprocess timeout cleanup."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import _subprocess_timeout
from cuprum._subprocess_execution import (
    _drain_stream_consumers,
    _wait_for_exit_code,
    _wait_for_exit_code_within_timeout,
)
from cuprum._subprocess_timeout import (
    _handle_stream_timeout,
    _SubprocessInvariantError,
    _SubprocessTimeoutError,
)
from cuprum.sh import ExecutionContext

if typ.TYPE_CHECKING:
    # Referenced only in annotations and string-based ``typ.cast`` below, so
    # these carry no runtime dependency and stay type-checking-only.
    from cuprum._pipeline_types import _EventDetails
    from cuprum._subprocess_execution import _SubprocessExecution

# A consumer either completes with captured text or fails with an exception.
_CONSUMER_OUTCOME = st.one_of(
    st.text(max_size=16).map(lambda value: ("value", value)),
    st.just(("raise", None)),
)


@dc.dataclass(frozen=True, slots=True)
class _DrainCase:
    """Generated scheduling and outcome configuration for a consumer drain."""

    stdout: tuple[str, str | None]
    stdout_delay: int
    stderr: tuple[str, str | None]
    stderr_delay: int


_DRAIN_CASE = st.builds(
    _DrainCase,
    stdout=_CONSUMER_OUTCOME,
    stdout_delay=st.integers(min_value=0, max_value=3),
    stderr=_CONSUMER_OUTCOME,
    stderr_delay=st.integers(min_value=0, max_value=3),
)


async def _consumer(outcome: tuple[str, str | None], delay: int) -> str | None:
    """Yield ``delay`` times, then either return text or fail."""
    for _ in range(delay):
        await asyncio.sleep(0)
    kind, value = outcome
    if kind == "raise":
        msg = "consumer failed"
        raise ValueError(msg)
    return value


def test_drain_stream_consumers_cancels_pending_and_decodes() -> None:
    """Draining cancels a still-pending consumer and preserves a completed one."""

    async def block() -> str | None:
        """Remain pending until the drain cancels this task."""
        await asyncio.Event().wait()

    async def run_case() -> None:
        """Drain a still-pending consumer alongside an already-completed one."""
        pending = asyncio.create_task(block())
        completed = asyncio.create_task(asyncio.sleep(0, result="stderr"))
        await completed  # the completed consumer must be genuinely done

        stdout_text, stderr_text = await _drain_stream_consumers((pending, completed))

        assert pending.cancelled(), (
            "a consumer left pending at drain must be cancelled, "
            f"but pending.done()={pending.done()}"
        )
        assert stdout_text is None, (
            f"a cancelled consumer must decode to None, not {stdout_text!r}"
        )
        assert stderr_text == "stderr", (
            f"a completed consumer's text must be preserved, got {stderr_text!r}"
        )

    asyncio.run(run_case())


# Number of event-loop turns the harness lets consumers settle before draining.
# ``_consumer`` needs ``delay + 1`` turns to reach its outcome, and each consumer
# is scheduled exactly once per turn, so a consumer whose ``delay`` is at least
# ``_SETTLE_TURNS`` is still pending when the drain runs (and must be cancelled
# and decoded to ``None``); one with a smaller delay has completed and keeps its
# outcome.
_SETTLE_TURNS = 2


@settings(max_examples=75, deadline=None, derandomize=True)
@given(case=_DRAIN_CASE)
def test_drain_stream_consumers_decodes_across_orderings(case: _DrainCase) -> None:
    """_drain_stream_consumers drains once, decoding completed and pending readers.

    Consumers settle for a fixed number of event-loop turns before the drain, so
    those whose delay fits the window complete (their text preserved, a failure
    mapping to ``None``) while those whose delay exceeds it are still pending at
    drain time and must be cancelled and decoded to ``None``.
    """

    async def run_case() -> None:
        """Drive the drain once and assert its decoding invariants."""
        consumers = (
            asyncio.create_task(_consumer(case.stdout, case.stdout_delay)),
            asyncio.create_task(_consumer(case.stderr, case.stderr_delay)),
        )
        # Let consumers settle for a bounded number of turns rather than awaiting
        # every one to completion, so readers slower than the window remain
        # pending when the drain runs and exercise its cancellation path.
        for _ in range(_SETTLE_TURNS):
            await asyncio.sleep(0)

        stdout_text, stderr_text = await _drain_stream_consumers(consumers)

        assert all(task.done() for task in consumers), (
            "every consumer task must be drained before the helper returns"
        )
        for label, task, captured, outcome, delay in (
            ("stdout", consumers[0], stdout_text, case.stdout, case.stdout_delay),
            ("stderr", consumers[1], stderr_text, case.stderr, case.stderr_delay),
        ):
            kind, value = outcome
            if delay >= _SETTLE_TURNS:
                assert task.cancelled(), (
                    f"{label} consumer with delay {delay} must still be pending at "
                    f"drain and be cancelled, but cancelled()={task.cancelled()}"
                )
                expected = None
            else:
                assert not task.cancelled(), (
                    f"{label} consumer with delay {delay} must complete within the "
                    "settling window rather than being cancelled"
                )
                expected = None if kind == "raise" else value
            assert captured == expected, (
                f"{label} capture must reflect its consumer outcome: "
                f"expected {expected!r}, got {captured!r}"
            )

    asyncio.run(run_case())


def test_handle_stream_timeout_preserves_timeout_and_output() -> None:
    """The handler raises with the configured timeout and pre-drained output."""
    with pytest.raises(_SubprocessTimeoutError) as exc_info:
        _handle_stream_timeout(
            TimeoutError(),
            stdout_text="captured-out",
            stderr_text=None,
            timeout=1.5,
        )

    assert exc_info.value.timeout == 1.5, (
        f"the configured timeout must survive unchanged: got {exc_info.value.timeout}"
    )
    assert exc_info.value.stdout == "captured-out", (
        "pre-drained stdout must be preserved on the timeout error, "
        f"got {exc_info.value.stdout!r}"
    )
    assert exc_info.value.stderr is None, (
        f"pre-drained stderr must be preserved, got {exc_info.value.stderr!r}"
    )


def test_handle_stream_timeout_requires_configured_timeout() -> None:
    """A timeout handler reached without a configured timeout fails loudly."""
    with pytest.raises(_SubprocessInvariantError):
        _handle_stream_timeout(
            TimeoutError(),
            stdout_text=None,
            stderr_text=None,
            timeout=None,
        )


class _TimeoutWaitProcess:
    """Process double whose ``wait()`` blocks until terminate()/kill()."""

    def __init__(self) -> None:
        """Start unexited, with a pid and unset started/termination events."""
        self.returncode: int | None = None
        self.pid = 4321
        self._exited = asyncio.Event()
        self.wait_started = asyncio.Event()

    async def wait(self) -> int:
        """Block until a termination signal records an exit code."""
        self.wait_started.set()
        await self._exited.wait()
        assert self.returncode is not None, (
            "process double invariant: terminate()/kill() must record "
            "returncode before _exited is set"
        )
        return self.returncode

    def terminate(self) -> None:
        """Record a SIGTERM exit code and release ``wait()``."""
        if self.returncode is None:
            self.returncode = -15
        self._exited.set()

    def kill(self) -> None:
        """Record a SIGKILL exit code and release ``wait()``."""
        if self.returncode is None:
            self.returncode = -9
        self._exited.set()


def test_wait_for_exit_code_terminates_process_on_timeout() -> None:
    """A timed-out wait terminates the process before propagating TimeoutError.

    ``_wait_for_exit_code`` waits only for the exit code now; on expiry it must
    still tear the process down so the caller's stream consumers can reach EOF,
    while the original ``TimeoutError`` propagates unchanged.
    """

    async def run_case() -> None:
        """Drive ``_wait_for_exit_code`` through its timeout-cleanup branch."""
        process = _TimeoutWaitProcess()
        ctx = ExecutionContext(cancel_grace=0.1)

        # The deadline now belongs to the caller: asyncio.timeout expiry cancels
        # the wait, driving the same cleanup branch and re-raising as
        # TimeoutError at the context-manager boundary.
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await _wait_for_exit_code(
                    typ.cast("asyncio.subprocess.Process", process),
                    ctx,
                )

        assert process.returncode == -15, (
            "the process must be terminated during timeout cleanup, "
            f"but returncode={process.returncode}"
        )

    asyncio.run(run_case())


def test_wait_for_exit_code_terminates_process_on_cancellation() -> None:
    """Cancelling the waiter terminates the process before CancelledError propagates.

    This covers the distinct ``asyncio.CancelledError`` cleanup path (not the
    timeout path): when the task running ``_wait_for_exit_code`` is cancelled
    mid-wait, cleanup must terminate the process and let ``CancelledError``
    propagate.
    """

    async def run_case() -> None:
        """Cancel ``_wait_for_exit_code`` mid-wait and assert its cleanup."""
        process = _TimeoutWaitProcess()
        ctx = ExecutionContext(cancel_grace=0.1)

        task = asyncio.create_task(
            _wait_for_exit_code(
                typ.cast("asyncio.subprocess.Process", process),
                ctx,
            )
        )
        # Wait until the task has actually entered process.wait() before
        # cancelling, so the cleanup branch is exercised deterministically
        # rather than racing a fixed sleep.
        await asyncio.wait_for(process.wait_started.wait(), timeout=2.0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert process.returncode == -15, (
            "the process must be terminated during cancellation cleanup, "
            f"but returncode={process.returncode}"
        )

    asyncio.run(run_case())


class _ExitedProcess:
    """Process double whose ``wait()`` returns immediately (already exited)."""

    def __init__(self, returncode: int = 0) -> None:
        """Start already exited, with a recorded exit code and a pid."""
        self.returncode = returncode
        self.pid = 5678

    async def wait(self) -> int:
        """Return the recorded exit code without ever suspending."""
        return self.returncode

    def terminate(self) -> None:
        """No-op: the process has already exited."""

    def kill(self) -> None:
        """No-op: the process has already exited."""


@dc.dataclass(slots=True)
class _RecordingObservation:
    """Observation double recording emitted ``(phase, _EventDetails)`` pairs."""

    events: list[tuple[str, _EventDetails]] = dc.field(default_factory=list)

    def emit(self, phase: str, details: _EventDetails) -> None:
        """Record an emitted observe event."""
        self.events.append((phase, details))


@dc.dataclass(slots=True)
class _RaisingObservation:
    """Observation double whose ``emit`` always raises, simulating a bad hook."""

    def emit(self, phase: str, details: _EventDetails) -> typ.NoReturn:
        """Raise as if an observe hook failed while handling the event."""
        _ = (phase, details)
        msg = "observe hook exploded"
        raise RuntimeError(msg)


@dc.dataclass(slots=True)
class _DeadlineExecution:
    """Execution stand-in exposing only the fields the waiter reads."""

    ctx: ExecutionContext
    timeout: float | None
    observation: _RecordingObservation = dc.field(default_factory=_RecordingObservation)


@pytest.mark.parametrize("timeout", [0, 0.0, -1.0])
def test_non_positive_timeout_expires_immediately(timeout: float) -> None:
    """A non-positive deadline times out even when the process already exited.

    ``asyncio.timeout`` schedules its cancellation only for the next event-loop
    iteration, so a process whose ``wait()`` returns without suspending would
    otherwise race past a zero or negative deadline and report success. The
    waiter must instead expire deterministically, preserving the previous
    ``asyncio.wait_for`` behaviour (regression for ``run(timeout=0)``).
    """

    async def run_case() -> None:
        """Drive the waiter with an already-exited process and expect a timeout."""
        process = _ExitedProcess()
        execution = _DeadlineExecution(ctx=ExecutionContext(), timeout=timeout)

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )

    asyncio.run(run_case())


def test_non_positive_timeout_terminates_running_process() -> None:
    """A non-positive deadline terminates a still-running process before raising.

    Stream consumers belong to the caller, which drains them exactly once via
    ``_drain_stream_consumers``; the fast path's own teardown duty is to
    terminate the process so those consumers can reach EOF during that drain.
    """

    async def run_case() -> None:
        """Expire immediately against a running process and assert termination."""
        process = _TimeoutWaitProcess()
        execution = _DeadlineExecution(
            ctx=ExecutionContext(cancel_grace=0.1), timeout=0
        )

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )

        assert process.returncode == -15, (
            "the process must be terminated when a non-positive deadline expires, "
            f"but returncode={process.returncode}"
        )

    asyncio.run(run_case())


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
    assert fields["cuprum_timeout_mode"] == "elapsed_deadline"
    assert fields["cuprum_operation"] == "wait"
    assert fields["cuprum_pid"] == 4321
    assert fields["cuprum_timeout_s"] == 0.05
    assert fields["cuprum_error_type"] == "TimeoutError"


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
    assert fields["cuprum_timeout_mode"] == "non_positive_immediate"
    assert fields["cuprum_operation"] == "wait"
    assert fields["cuprum_pid"] == 5678
    assert fields["cuprum_timeout_s"] == 0
    assert fields["cuprum_error_type"] == "TimeoutError"


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
    assert fields["cuprum_operation"] == "teardown"
    assert fields["cuprum_teardown_outcome"] == "drain_error"
    assert fields["cuprum_pid"] == 5678
    assert "ValueError" in fields["cuprum_error_type"]


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
        execution = _DeadlineExecution(
            ctx=ExecutionContext(cancel_grace=0.1), timeout=0.05
        )

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )
        return execution.observation

    observation = asyncio.run(run_case())
    details = _single_timeout_event(observation)
    assert details.timeout_mode == "elapsed_deadline"
    assert details.operation == "wait"
    assert details.timeout_s == 0.05
    assert details.error_type == "TimeoutError"
    assert details.pid == 4321


def test_non_positive_timeout_emits_observe_event() -> None:
    """The immediate fast path emits a ``timeout`` event tagged non-positive."""

    async def run_case() -> _RecordingObservation:
        """Trigger the immediate fast path and return the recorded observation."""
        process = _ExitedProcess()
        execution = _DeadlineExecution(ctx=ExecutionContext(), timeout=0)

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )
        return execution.observation

    observation = asyncio.run(run_case())
    details = _single_timeout_event(observation)
    assert details.timeout_mode == "non_positive_immediate"
    assert details.operation == "wait"
    assert details.timeout_s == 0
    assert details.error_type == "TimeoutError"
    assert details.pid == 5678


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
            pid=5678,
            observation=typ.cast("typ.Any", observation),
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
    assert details.operation == "drain"
    assert details.pid == 5678
    assert details.error_type is not None
    assert "ValueError" in details.error_type


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
            observation=typ.cast("_RecordingObservation", _RaisingObservation()),
        )

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )

    asyncio.run(run_case())
