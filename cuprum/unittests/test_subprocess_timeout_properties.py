"""Property coverage for the timeout waiter and the consumer drain.

``test_subprocess_timeout`` pins these paths at hand-picked values. These
generalise over the input domain instead, so an invariant that happens to hold
at ``0`` and ``-1.0`` cannot pass by coincidence. It lives apart from that
module because mixing example-based and generated cases there would obscure
both, and because the module is already close to the project's line ceiling.

Nothing here spawns a subprocess: every case drives a deterministic double.
Where a positive deadline must elapse, the double's ``wait()`` never completes,
so expiry is the only reachable outcome rather than a race against the clock.
Where a positive deadline must *not* elapse, the double has already exited and
``wait()`` returns without suspending, so the deadline is never approached.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum._subprocess_execution import (
    _drain_stream_consumers,
    _wait_for_exit_code_within_timeout,
)
from cuprum.sh import ExecutionContext

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._subprocess_execution import _SubprocessExecution

_SIGTERM_CODE = -15
_EXAMPLES = 25

# Generous enough that an already-exited process always wins the race by
# construction: its ``wait()`` returns without ever suspending.
_GENEROUS = st.floats(
    min_value=0.5, max_value=30.0, allow_nan=False, allow_infinity=False
)
# Small and bounded. Paired only with a process that never exits, so expiry is
# the sole outcome; the bound keeps the suite fast rather than deciding it.
_ELAPSING = st.floats(
    min_value=0.001, max_value=0.02, allow_nan=False, allow_infinity=False
)
_NON_POSITIVE = st.floats(
    min_value=-30.0, max_value=0.0, allow_nan=False, allow_infinity=False
)
_GRACE = st.floats(min_value=0.0, max_value=0.2, allow_nan=False, allow_infinity=False)


class _ExitedProcess:
    """Double that has already exited; ``wait()`` never suspends."""

    def __init__(self, returncode: int = 0) -> None:
        """Record the exit code and start with an empty call log."""
        self.returncode = returncode
        self.pid = 5678
        self.calls: list[str] = []

    async def wait(self) -> int:
        """Return the exit code immediately, logging the call."""
        self.calls.append("wait")
        return self.returncode

    def terminate(self) -> None:
        """Log the call; the process has already exited."""
        self.calls.append("terminate")

    def kill(self) -> None:
        """Log the call; the process has already exited."""
        self.calls.append("kill")


class _RunningProcess:
    """Double that blocks in ``wait()`` until terminated or killed."""

    def __init__(self) -> None:
        """Start unexited, exposing entry and exit signals for coordination."""
        self.returncode: int | None = None
        self.pid = 4321
        self.calls: list[str] = []
        self.wait_started = asyncio.Event()
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        """Block until ``terminate()``/``kill()`` records an exit code."""
        self.calls.append("wait")
        self.wait_started.set()
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        """Record a SIGTERM exit code and release ``wait()``."""
        self.calls.append("terminate")
        if self.returncode is None:
            self.returncode = _SIGTERM_CODE
        self._exited.set()

    def kill(self) -> None:
        """Record a SIGKILL exit code and release ``wait()``."""
        self.calls.append("kill")
        if self.returncode is None:
            self.returncode = -9
        self._exited.set()


class _NullObservation:
    """Observation double that discards every event emitted through it.

    Carried so this module stays valid on the stacked observability branch,
    where the waiter reports expiries through ``execution.observation``. The
    invariants asserted here are about teardown and exception flow, not about
    what was emitted, so discarding keeps them independent of that layer.
    """

    def emit(self, phase: str, details: object) -> None:
        """Discard the emitted event."""


@dc.dataclass(frozen=True, slots=True)
class _Execution:
    """Execution stand-in exposing only the fields the waiter reads."""

    ctx: ExecutionContext
    timeout: float | None
    observation: _NullObservation = dc.field(default_factory=_NullObservation)


def _execution(configured: float | None, grace: float) -> _SubprocessExecution:
    """Build the execution stand-in the waiter expects."""
    return typ.cast(
        "_SubprocessExecution",
        _Execution(ctx=ExecutionContext(cancel_grace=grace), timeout=configured),
    )


def _wait(
    process: object, execution: _SubprocessExecution
) -> cabc.Coroutine[typ.Any, typ.Any, tuple[int, float]]:
    """Invoke the waiter with the doubles cast to the declared types."""
    return _wait_for_exit_code_within_timeout(
        typ.cast("asyncio.subprocess.Process", process), execution
    )


# -- Invariant 1: ``None`` applies no deadline --------------------------------


@settings(deadline=None, max_examples=_EXAMPLES)
@given(grace=_GRACE)
def test_none_timeout_applies_no_deadline(grace: float) -> None:
    """A ``None`` timeout never expires, however long the process runs.

    Driven by yielding to the loop repeatedly rather than by sleeping: if a
    deadline were applied at all, the waiter would have to settle, and it must
    not.
    """

    async def run_case() -> None:
        """Leave the waiter pending, then release it by terminating."""
        process = _RunningProcess()
        task = asyncio.create_task(_wait(process, _execution(None, grace)))
        await process.wait_started.wait()
        for _ in range(10):
            await asyncio.sleep(0)
        assert not task.done(), (
            "a None timeout must not settle the waiter, but it completed with "
            f"{task.exception() if task.done() else None!r}"
        )
        process.terminate()
        exit_code, _ = await task
        assert exit_code == _SIGTERM_CODE

    asyncio.run(run_case())


# -- Invariant 2: a positive deadline permits completion ----------------------


@settings(deadline=None, max_examples=_EXAMPLES)
@given(configured=_GENEROUS, grace=_GRACE, code=st.integers(min_value=0, max_value=255))
def test_positive_timeout_allows_completion(
    configured: float, grace: float, code: int
) -> None:
    """A process that exits before the deadline returns its code normally."""

    async def run_case() -> None:
        """Await an already-exited process under a generous deadline."""
        process = _ExitedProcess(returncode=code)
        exit_code, _ = await _wait(process, _execution(configured, grace))
        assert exit_code == code, (
            f"a completed wait must return the process exit code {code}, got "
            f"{exit_code}"
        )

    asyncio.run(run_case())


# -- Invariant 3: an elapsed positive deadline terminates ---------------------


@settings(deadline=None, max_examples=_EXAMPLES)
@given(configured=_ELAPSING, grace=_GRACE)
def test_elapsed_positive_timeout_terminates_running_process(
    configured: float, grace: float
) -> None:
    """An elapsed deadline terminates the process before ``TimeoutError``."""

    async def run_case() -> None:
        """Let a bounded deadline elapse against a process that never exits."""
        process = _RunningProcess()
        with pytest.raises(TimeoutError):
            await _wait(process, _execution(configured, grace))
        assert process.returncode == _SIGTERM_CODE, (
            "an elapsed deadline must terminate the process before the "
            f"TimeoutError propagates, got returncode={process.returncode!r}"
        )

    asyncio.run(run_case())


# -- Invariants 4 and 5: non-positive deadlines -------------------------------


@settings(deadline=None, max_examples=_EXAMPLES)
@given(configured=_NON_POSITIVE, grace=_GRACE)
def test_non_positive_timeout_terminates_without_awaiting_wait(
    configured: float, grace: float
) -> None:
    """A non-positive deadline terminates without ever awaiting ``wait()``.

    The fast path exists precisely because ``asyncio.timeout`` would schedule
    its cancellation for the next loop iteration, letting a non-suspending
    ``wait()`` race past an already-elapsed deadline.

    Termination itself legitimately awaits ``wait()`` to reap the child, so the
    invariant is one of *order*: ``terminate`` must come first. A deadline wait
    would have logged ``wait`` before it.
    """

    async def run_case() -> None:
        """Expire immediately against a running process."""
        process = _RunningProcess()
        with pytest.raises(TimeoutError):
            await _wait(process, _execution(configured, grace))
        assert process.calls[0] == "terminate", (
            "the fast path must terminate before any wait; a leading 'wait' "
            f"means the deadline was awaited, got calls={process.calls!r}"
        )
        assert process.returncode == _SIGTERM_CODE, (
            "the fast path must still terminate the process, got "
            f"returncode={process.returncode!r}"
        )

    asyncio.run(run_case())


@settings(deadline=None, max_examples=_EXAMPLES)
@given(configured=_NON_POSITIVE, grace=_GRACE, code=st.integers(0, 255))
def test_non_positive_timeout_expires_for_exited_process(
    configured: float, grace: float, code: int
) -> None:
    """A non-positive deadline expires even when the process already exited.

    This is the regression the fast path was introduced for: without it, a
    ``wait()`` that returns without suspending reports success.
    """

    async def run_case() -> None:
        """Expire immediately against an already-exited process."""
        process = _ExitedProcess(returncode=code)
        with pytest.raises(TimeoutError):
            await _wait(process, _execution(configured, grace))
        assert process.calls == [], (
            "an already-exited process needs no teardown, and the fast path "
            f"must not await its wait() either, got calls={process.calls!r}"
        )

    asyncio.run(run_case())


# -- Invariant 6: external cancellation ---------------------------------------


@settings(deadline=None, max_examples=_EXAMPLES)
@given(configured=st.none() | _GENEROUS, grace=_GRACE)
def test_external_cancellation_terminates_and_reraises(
    configured: float | None, grace: float
) -> None:
    """Cancelling the waiter terminates the process and re-raises.

    Cancellation is requested only once ``wait()`` has been entered, so the
    waiter is genuinely suspended rather than cancelled before it starts.
    """

    async def run_case() -> None:
        """Cancel a suspended waiter and assert teardown and propagation."""
        process = _RunningProcess()
        task = asyncio.create_task(_wait(process, _execution(configured, grace)))
        await process.wait_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode == _SIGTERM_CODE, (
            "cancellation must terminate the process, got "
            f"returncode={process.returncode!r}"
        )

    asyncio.run(run_case())


# -- Invariants 7 and 8: the consumer drain -----------------------------------


class _ConsumerFailureError(RuntimeError):
    """Raised by a stream-consumer double that fails during the drain."""


def _make_consumer(
    kind: str, text: str
) -> cabc.Coroutine[typ.Any, typ.Any, str | None]:
    """Return a consumer coroutine of the requested behaviour."""

    async def completed() -> str | None:
        """Yield once so the task finishes, then return decoded text."""
        await asyncio.sleep(0)
        return text

    async def pending() -> str | None:
        """Block until cancelled by the drain."""
        await asyncio.Event().wait()
        return text

    async def failing() -> str | None:
        """Yield once, then fail with an error the drain must absorb."""
        await asyncio.sleep(0)
        raise _ConsumerFailureError

    return {"completed": completed, "pending": pending, "failing": failing}[kind]()


_CONSUMER_KINDS = st.sampled_from(("completed", "pending", "failing"))


async def _drain_while_raising(
    primary: BaseException,
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
) -> None:
    """Drain ``consumers`` while ``primary`` is propagating, as cleanup does."""
    try:
        raise primary
    finally:
        await _drain_stream_consumers(consumers)


@settings(deadline=None, max_examples=_EXAMPLES)
@given(
    stdout_kind=_CONSUMER_KINDS,
    stderr_kind=_CONSUMER_KINDS,
    text=st.text(max_size=16),
)
def test_drain_leaves_no_pending_consumer(
    stdout_kind: str, stderr_kind: str, text: str
) -> None:
    """The drain settles both consumers whatever state they were in.

    Draining owns the consumers on this path, so nothing may be left pending
    afterwards; a stranded reader would outlive the run it belonged to.
    """

    async def run_case() -> None:
        """Drain an arbitrary pair of consumers and inspect what remains."""
        consumers = (
            asyncio.create_task(_make_consumer(stdout_kind, text)),
            asyncio.create_task(_make_consumer(stderr_kind, text)),
        )
        # A task the loop has not scheduled yet is indistinguishable from a
        # blocked one, and the drain cancels both. Yield until the finishing
        # consumers have settled so "completed" means what it says.
        for _ in range(4):
            await asyncio.sleep(0)
        stdout_text, stderr_text = await _drain_stream_consumers(consumers)

        for task, kind, value in (
            (consumers[0], stdout_kind, stdout_text),
            (consumers[1], stderr_kind, stderr_text),
        ):
            assert task.done(), f"the {kind} consumer was left pending"
            expected = text if kind == "completed" else None
            assert value == expected, (
                f"a {kind} consumer must decode to {expected!r}, got {value!r}"
            )

    asyncio.run(run_case())


@settings(deadline=None, max_examples=_EXAMPLES)
@given(stdout_kind=_CONSUMER_KINDS, stderr_kind=_CONSUMER_KINDS)
def test_drain_absorbs_failures_without_replacing_primary_error(
    stdout_kind: str, stderr_kind: str
) -> None:
    """A failing consumer cannot displace the error being cleaned up after.

    The drain runs while a timeout or cancellation is already propagating, so
    it must absorb consumer failures rather than raise its own. Draining inside
    an active ``TimeoutError`` handler asserts exactly that: the original error
    is what leaves the block.
    """

    async def run_case() -> None:
        """Drain failing consumers while a TimeoutError is propagating."""
        consumers = (
            asyncio.create_task(_make_consumer(stdout_kind, "out")),
            asyncio.create_task(_make_consumer(stderr_kind, "err")),
        )
        primary = TimeoutError("primary")
        with pytest.raises(TimeoutError) as exc_info:
            await _drain_while_raising(primary, consumers)
        assert exc_info.value is primary, (
            f"the drain must not replace the propagating error, got {exc_info.value!r}"
        )

    asyncio.run(run_case())
