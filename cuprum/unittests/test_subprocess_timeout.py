"""Unit tests for subprocess timeout cleanup."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum._subprocess_execution import _wait_for_exit_code
from cuprum._subprocess_timeout import (
    _handle_stream_timeout,
    _SubprocessTimeoutError,
)
from cuprum.sh import ExecutionContext

# A consumer either completes with captured text or fails with an exception.
_CONSUMER_OUTCOME = st.one_of(
    st.text(max_size=16).map(lambda value: ("value", value)),
    st.just(("raise", None)),
)


@dc.dataclass(frozen=True, slots=True)
class _StreamTimeoutCase:
    """Generated scheduling and outcome configuration for timeout cleanup."""

    stdout: tuple[str, str | None]
    stdout_delay: int
    stderr: tuple[str, str | None]
    stderr_delay: int
    stdin_delay: int
    timeout: float


_STREAM_TIMEOUT_CASE = st.builds(
    _StreamTimeoutCase,
    stdout=_CONSUMER_OUTCOME,
    stdout_delay=st.integers(min_value=0, max_value=3),
    stderr=_CONSUMER_OUTCOME,
    stderr_delay=st.integers(min_value=0, max_value=3),
    stdin_delay=st.integers(min_value=0, max_value=3),
    timeout=st.floats(min_value=0.001, max_value=3600.0, allow_nan=False),
)


def test_stream_timeout_preserves_timeout_when_consumer_fails() -> None:
    """A stream-consumer failure cannot mask a timeout during cleanup."""

    async def block_stdin() -> None:
        """Remain pending until timeout cleanup cancels the task."""
        await asyncio.Event().wait()

    async def fail_consumer() -> str | None:
        """Yield once, then raise the stream-consumer failure."""
        await asyncio.sleep(0)
        msg = "consumer failed"
        raise RuntimeError(msg)

    async def handle_timeout() -> None:
        """Run the timeout handler and assert its cleanup outcome."""
        stdin_task = asyncio.create_task(block_stdin())
        consumers = (
            asyncio.create_task(fail_consumer()),
            asyncio.create_task(asyncio.sleep(0, result="stderr")),
        )

        with pytest.raises(_SubprocessTimeoutError) as exc_info:
            await _handle_stream_timeout(
                TimeoutError(),
                stdin_task=stdin_task,
                consumers=consumers,
                timeout=1.0,
            )

        assert stdin_task.cancelled(), (
            "timeout cleanup must cancel the pending stdin writer, "
            f"but stdin_task.done()={stdin_task.done()} and it was not cancelled"
        )
        assert exc_info.value.stdout is None, (
            "a failed stdout consumer must surface as None, "
            f"not {exc_info.value.stdout!r}"
        )
        assert exc_info.value.stderr == "stderr", (
            "a successful stderr consumer must be preserved through timeout "
            f"cleanup, but got {exc_info.value.stderr!r}"
        )

    asyncio.run(handle_timeout())


@settings(max_examples=75, deadline=None, derandomize=True)
@given(case=_STREAM_TIMEOUT_CASE)
def test_handle_stream_timeout_upholds_invariants_across_orderings(
    case: _StreamTimeoutCase,
) -> None:
    """_handle_stream_timeout keeps its cleanup contract for any task ordering.

    Across arbitrary interleavings of consumer completion, consumer failure,
    and a stdin writer that blocks until cancelled, the handler must always:

    - raise ``_SubprocessTimeoutError`` carrying the configured timeout,
    - cancel and drain the pending stdin writer, and
    - surface each successful consumer's text while mapping a failed consumer
      to ``None``.
    """

    async def consumer(outcome: tuple[str, str | None], delay: int) -> str | None:
        """Yield ``delay`` times, then either return text or fail."""
        for _ in range(delay):
            await asyncio.sleep(0)
        kind, value = outcome
        if kind == "raise":
            msg = "consumer failed"
            raise RuntimeError(msg)
        return value

    async def block_stdin() -> None:
        """Yield ``case.stdin_delay`` times, then block until cancelled."""
        for _ in range(case.stdin_delay):
            await asyncio.sleep(0)
        await asyncio.Event().wait()

    async def run_case() -> None:
        """Drive the handler once and assert its cleanup invariants."""
        stdin_task = asyncio.create_task(block_stdin())
        consumers = (
            asyncio.create_task(consumer(case.stdout, case.stdout_delay)),
            asyncio.create_task(consumer(case.stderr, case.stderr_delay)),
        )

        with pytest.raises(_SubprocessTimeoutError) as exc_info:
            await _handle_stream_timeout(
                TimeoutError(),
                stdin_task=stdin_task,
                consumers=consumers,
                timeout=case.timeout,
            )

        exc = exc_info.value
        assert exc.timeout == case.timeout, (
            f"timeout must survive cleanup unchanged: got {exc.timeout} "
            f"for configured {case.timeout}"
        )
        assert stdin_task.cancelled(), (
            "the blocked stdin writer must be cancelled during cleanup, "
            f"but stdin_task.done()={stdin_task.done()}"
        )
        assert all(task.done() for task in consumers), (
            "every consumer task must be drained before the handler returns"
        )
        for label, captured, outcome in (
            ("stdout", exc.stdout, case.stdout),
            ("stderr", exc.stderr, case.stderr),
        ):
            kind, value = outcome
            expected = None if kind == "raise" else value
            assert captured == expected, (
                f"{label} capture must reflect its consumer outcome: "
                f"expected {expected!r}, got {captured!r}"
            )

    asyncio.run(run_case())


class _TimeoutWaitProcess:
    """Process double whose ``wait()`` blocks until terminate()/kill()."""

    def __init__(self) -> None:
        """Start unexited, with a pid and an unset termination event."""
        self.returncode: int | None = None
        self.pid = 4321
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        """Block until a termination signal records an exit code."""
        await self._exited.wait()
        assert self.returncode is not None
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


def test_wait_for_exit_code_cancels_pending_consumers_on_timeout() -> None:
    """A timed-out wait cancels and drains consumers still pending after kill.

    The stream path hands its stdout/stderr tasks to ``_wait_for_exit_code`` as
    ``consumers``. When ``process.wait()`` times out and a consumer remains
    blocked after termination, cleanup must cancel and drain it so timeout
    handling cannot hang, while the original ``TimeoutError`` still propagates.
    """

    async def blocking_consumer() -> None:
        """Block until timeout cleanup cancels this task."""
        await asyncio.Event().wait()

    async def run_case() -> None:
        """Drive ``_wait_for_exit_code`` through its timeout-cleanup branch."""
        process = _TimeoutWaitProcess()
        consumer = asyncio.create_task(blocking_consumer())
        ctx = ExecutionContext(cancel_grace=0.1)

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code(
                typ.cast("asyncio.subprocess.Process", process),
                ctx,
                timeout=0.05,
                consumers=(consumer,),
            )

        assert consumer.cancelled(), (
            "a consumer left pending at timeout must be cancelled during "
            f"cleanup, but consumer.done()={consumer.done()}"
        )
        assert consumer.done(), (
            "the cancelled consumer must be drained before the timeout propagates"
        )

    asyncio.run(run_case())
