"""Unit contracts for failed ``SafeCmd.lines()`` exit cleanup."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import types
import typing as typ

import pytest

from cuprum._line_stream import coordinator
from cuprum.capture_policy import BoundedCapture, FullCapture
from cuprum.line_stream_events import LineStreamPhase
from cuprum.unittests._stream_drain_support import captured

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._line_stream import _LineStreamRun
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait import _DrainContext, _DrainPair
    from cuprum.capture_policy import CapturePolicy


class _TimeoutHandledError(Exception):
    """Sentinel raised by the patched timeout translator."""


_STDOUT_DRAIN = captured("stdout text")
_STDERR_DRAIN = captured("stderr text")


@dc.dataclass(slots=True)
class _FailedExitTestDouble:
    """Patchable failed-exit seams and the observations they record.

    One instance supplies every seam the exit path awaits, so a case's recorded
    contexts, translated timeout output, and lifecycle order are read from the
    same object that produced them.

    Attributes
    ----------
    error
        The failure the patched exit wait raises.
    contexts
        The drain contexts the patched reconciliation received, in order.
    timeout_calls
        The translated timeout output recorded per call.
    order
        Lifecycle phases and the drain markers, in occurrence order.
    task_sets
        The task set each patched drain seam received, in order.
    """

    error: BaseException
    contexts: list[_DrainContext] = dc.field(default_factory=list)
    timeout_calls: list[tuple[str | None, str | None]] = dc.field(default_factory=list)
    order: list[object] = dc.field(default_factory=list)
    task_sets: list[object] = dc.field(default_factory=list)

    def emit(self, phase: LineStreamPhase, _details: object = None) -> None:
        """Record one lifecycle phase in its emission order."""
        self.order.append(phase)

    async def wait_for_exit(
        self,
        _process: asyncio.subprocess.Process,
        _execution: _SubprocessExecution,
    ) -> tuple[int, float]:
        """Raise the parametrized exit-path failure."""
        await asyncio.sleep(0)
        raise self.error

    async def reconcile(
        self,
        tasks: object,
        context: _DrainContext,
    ) -> _DrainPair:
        """Record the reconciled ownership and its cleanup context."""
        return await self._record("reconcile", tasks, context)

    async def drain(
        self,
        tasks: object,
        context: _DrainContext,
    ) -> _DrainPair:
        """Record the drained consumers and their cleanup context."""
        return await self._record("drain", tasks, context)

    async def _record(
        self,
        marker: str,
        tasks: object,
        context: _DrainContext,
    ) -> _DrainPair:
        """Record one drain seam invocation and produce captured output.

        The result is a whole drain pair rather than two bare payloads: the
        seams under test hand the line-stream coordinator everything a
        consumer settled to, and the coordinator reads the payload off it to
        build the ``TimeoutExpired``. Returning bare strings here would let a
        seam that discarded the record — and with it the spill handle — pass
        this module's assertions.

        Returns
        -------
        _DrainPair
            The canned stdout and stderr drain results, in that order.
        """
        self.task_sets.append(tasks)
        self.contexts.append(context)
        self.order.append(marker)
        await asyncio.sleep(0)
        return _STDOUT_DRAIN, _STDERR_DRAIN

    async def shield(
        self,
        operation: cabc.Awaitable[_DrainPair],
    ) -> _DrainPair:
        """Await the patched cleanup operation without changing its outcome."""
        return await operation

    def handle_timeout(
        self,
        _error: TimeoutError,
        *,
        stdout_text: str | None,
        stderr_text: str | None,
        timeout: float | None,
    ) -> None:
        """Record translated timeout output and stop the test path."""
        del timeout
        self.timeout_calls.append((stdout_text, stderr_text))
        raise _TimeoutHandledError

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch the failed-exit seams this double supplies."""
        monkeypatch.setattr(
            coordinator, "_wait_for_exit_code_within_timeout", self.wait_for_exit
        )
        monkeypatch.setattr(coordinator, "_reconcile_run_tasks", self.reconcile)
        monkeypatch.setattr(coordinator, "_drain_stream_consumers", self.drain)
        monkeypatch.setattr(coordinator, "_shielded_cleanup", self.shield)
        monkeypatch.setattr(coordinator, "_handle_stream_timeout", self.handle_timeout)

    def run_timeout_exit(
        self,
        run: _LineStreamRun,
        execution: _SubprocessExecution,
    ) -> None:
        """Drive the timeout path, which ends in the translator sentinel."""
        with pytest.raises(_TimeoutHandledError):
            asyncio.run(coordinator._wait_for_line_stream_exit(run, execution))

    def run_failed_exit(
        self,
        run: _LineStreamRun,
        execution: _SubprocessExecution,
    ) -> BaseException:
        """Drive a non-timeout failure and return the recorded exception."""
        with pytest.raises(type(self.error)) as caught:
            asyncio.run(coordinator._wait_for_line_stream_exit(run, execution))
        return caught.value

    def assert_cleanup_contract(
        self,
        *,
        expected_captures: tuple[CapturePolicy | None, CapturePolicy | None],
        expected_phases: tuple[LineStreamPhase, ...],
    ) -> None:
        """Assert the per-stream capture policies, PID, phases, and teardown order."""
        contexts = self.contexts
        order = self.order
        phases = [entry for entry in order if isinstance(entry, LineStreamPhase)]
        assert [context.captures for context in contexts] == [expected_captures], (
            f"cleanup must use the outcome capture policies, got {contexts!r}"
        )
        assert contexts[0].pid == 123, (
            f"cleanup must retain the child PID, got {contexts!r}"
        )
        assert phases == list(expected_phases), (
            f"failed exit must emit the expected lifecycle phases, got {phases!r}"
        )
        started_at = order.index(LineStreamPhase.TEARDOWN_STARTED)
        reconciled_at = order.index("reconcile")
        completed_at = order.index(LineStreamPhase.TEARDOWN_COMPLETED)
        assert started_at < reconciled_at, (
            f"teardown must start before reconciliation, got {order!r}"
        )
        assert reconciled_at < completed_at, (
            f"teardown completion must follow reconciliation, got {order!r}"
        )


def _failed_run(double: _FailedExitTestDouble) -> _LineStreamRun:
    """Build the smallest run shape the failed-exit cleanup needs.

    ``stdin_task`` is ``None`` so the exit race takes its no-writer branch and
    drives the patched wait directly: this module's cases are about what a
    *failed exit wait* does, not about the race, which
    ``test_safe_cmd_stdin_stream.py`` covers against a real child.

    Returns
    -------
    _LineStreamRun
        The minimal run, cast from a namespace to the shape the noexcept
        cleanup path reads.
    """
    run = types.SimpleNamespace(
        process=types.SimpleNamespace(pid=123),
        tasks=types.SimpleNamespace(discard_on_cancel=asyncio.Event(), stdin_task=None),
        telemetry=types.SimpleNamespace(emit=double.emit),
    )
    return typ.cast("_LineStreamRun", run)


_STDOUT_POLICY = FullCapture()
_STDERR_POLICY = BoundedCapture(max_bytes=64)
# What a route that preserves no output hands to the drain, per stream.
_NOTHING_PRESERVED: tuple[CapturePolicy | None, CapturePolicy | None] = (None, None)
# What a preserving route hands over: the run's own policies, per stream, in
# the stdout-then-stderr order every drain context uses.
_PRESERVED = (_STDOUT_POLICY, _STDERR_POLICY)


@pytest.mark.parametrize(
    ("error", "expected_captures", "expected_phases"),
    [
        (
            TimeoutError("deadline expired"),
            _PRESERVED,
            (
                LineStreamPhase.TIMEOUT,
                LineStreamPhase.TEARDOWN_STARTED,
                LineStreamPhase.TEARDOWN_COMPLETED,
            ),
        ),
        (
            asyncio.CancelledError(),
            _NOTHING_PRESERVED,
            (
                LineStreamPhase.CANCELLED,
                LineStreamPhase.TEARDOWN_STARTED,
                LineStreamPhase.TEARDOWN_COMPLETED,
            ),
        ),
        (
            ValueError("consumer failed"),
            _NOTHING_PRESERVED,
            (
                LineStreamPhase.TEARDOWN_STARTED,
                LineStreamPhase.TEARDOWN_COMPLETED,
            ),
        ),
    ],
)
def test_failed_line_stream_exit_reconciles_with_the_outcome_capture_policy(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected_captures: tuple[CapturePolicy | None, CapturePolicy | None],
    expected_phases: tuple[LineStreamPhase, ...],
) -> None:
    """Timeout captures output; cancellation and failures discard it.

    The two streams carry *different* policies in the preserving arm, so an
    implementation that collapsed them to one flag — the shape this replaces —
    could not satisfy the expectation.
    """
    double = _FailedExitTestDouble(error=error)
    run = _failed_run(double)
    execution = typ.cast(
        "_SubprocessExecution",
        types.SimpleNamespace(
            capture_stdout=_STDOUT_POLICY,
            capture_stderr=_STDERR_POLICY,
            capture_bytes=False,
            observation=object(),
            timeout=0.5,
        ),
    )
    double.install(monkeypatch)

    if isinstance(error, TimeoutError):
        double.run_timeout_exit(run, execution)
        assert double.timeout_calls == [("stdout text", "stderr text")], (
            "timeout translation must retain each reconciled drain's payload, "
            f"got {double.timeout_calls!r}"
        )
    else:
        caught = double.run_failed_exit(run, execution)
        assert caught is error, (
            f"failed exit must preserve its original exception, got {caught!r}"
        )

    double.assert_cleanup_contract(
        expected_captures=expected_captures,
        expected_phases=expected_phases,
    )


def test_discard_drain_keeps_its_supplied_pid_and_never_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discard drain drains the consumers without capturing output."""
    double = _FailedExitTestDouble(error=ValueError("unused"))
    consumers = object()
    run = typ.cast(
        "_LineStreamRun",
        types.SimpleNamespace(
            tasks=types.SimpleNamespace(
                consumers=consumers,
                discard_on_cancel=asyncio.Event(),
                # The discard drain now releases the accumulators the
                # consumers were reading into, so this case has to supply the
                # (empty) pair a run with no capture policies holds.
                captures=(None, None),
            ),
            telemetry=types.SimpleNamespace(emit=double.emit),
        ),
    )
    observation = object()
    execution = typ.cast(
        "_SubprocessExecution",
        types.SimpleNamespace(
            capture_stdout=_STDOUT_POLICY,
            capture_stderr=_STDERR_POLICY,
            capture_bytes=False,
            observation=observation,
        ),
    )
    double.install(monkeypatch)

    result = asyncio.run(coordinator._discard_drain(run, 456, execution))

    assert result == (_STDOUT_DRAIN, _STDERR_DRAIN), (
        f"the discard drain must return its drain result, got {result!r}"
    )
    assert double.task_sets == [consumers], (
        f"the discard drain must drain the consumer tasks, got {double.task_sets!r}"
    )
    context = double.contexts[0]
    assert context.captures == (None, None), (
        "the discard drain must preserve no stream's capture, even though the "
        f"execution carries policies; got {context.captures!r}"
    )
    assert context.pid == 456, (
        f"the discard drain must keep its supplied PID, got {context.pid!r}"
    )
    assert context.observation is observation, (
        f"the discard drain must retain the stage observation, got {context!r}"
    )
    assert context.discard_on_cancel is run.tasks.discard_on_cancel, (
        f"the discard drain must retain the discard signal, got {context!r}"
    )
    assert double.order == [
        LineStreamPhase.TEARDOWN_STARTED,
        "drain",
        LineStreamPhase.TEARDOWN_COMPLETED,
    ], f"the discard drain must bracket the drain in teardown, got {double.order!r}"


class _ReleasedSink:
    """A capture sink that records its release."""

    def __init__(self) -> None:
        """Start unreleased."""
        self.released = False

    def absorb(self, chunk: bytes) -> None:
        """Accept output, which this case never sends."""

    def finalize(self) -> typ.NoReturn:
        """Refuse to finalize; the discard path builds no result."""
        raise NotImplementedError

    async def cleanup(self) -> None:
        """Record the release."""
        self.released = True


def test_discard_drain_releases_the_capture_sinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discard path owns the sink release, since it bypasses reconciliation.

    ``_reconcile_run_tasks`` is where a normal teardown closes the
    accumulators, and the drain context here asks for no capture at all, so a
    consumer cancelled mid-read never finalizes and nothing else would close a
    spill file. This case fails without the ``finally`` in ``_discard_drain``.
    """
    double = _FailedExitTestDouble(error=ValueError("unused"))
    sink = _ReleasedSink()
    run = typ.cast(
        "_LineStreamRun",
        types.SimpleNamespace(
            tasks=types.SimpleNamespace(
                consumers=object(),
                discard_on_cancel=asyncio.Event(),
                captures=(sink, None),
            ),
            telemetry=types.SimpleNamespace(emit=double.emit),
        ),
    )
    execution = typ.cast(
        "_SubprocessExecution",
        types.SimpleNamespace(
            capture_stdout=_STDOUT_POLICY,
            capture_stderr=_STDERR_POLICY,
            capture_bytes=False,
            observation=object(),
        ),
    )
    double.install(monkeypatch)

    asyncio.run(coordinator._discard_drain(run, 456, execution))

    assert sink.released, (
        "the discard path drains no settled result, so it must release the "
        "accumulator itself or the spill file survives the run"
    )


def _teardown_run(order: list[object]) -> _LineStreamRun:
    """Build the minimal run shape the teardown wrapper needs."""

    def emit(phase: LineStreamPhase, _details: object = None) -> None:
        """Record one lifecycle phase in its emission order."""
        order.append(phase)

    return typ.cast(
        "_LineStreamRun",
        types.SimpleNamespace(telemetry=types.SimpleNamespace(emit=emit)),
    )


def test_teardown_wrapper_brackets_a_successful_cleanup() -> None:
    """Teardown starts before the operation and completes after it resolves."""
    order: list[object] = []

    async def exercise() -> None:
        """Run one successful teardown between the wrapper's boundaries."""

        async def operation() -> str:
            """Record the operation between the wrapper's two boundaries."""
            order.append("operation")
            await asyncio.sleep(0)
            return "captured"

        await coordinator._run_line_stream_teardown(
            _teardown_run(order),
            operation(),
        )

    asyncio.run(exercise())

    assert order == [
        LineStreamPhase.TEARDOWN_STARTED,
        "operation",
        LineStreamPhase.TEARDOWN_COMPLETED,
    ], f"teardown must bracket a successful cleanup, got {order!r}"


def test_teardown_wrapper_returns_the_operation_result_unchanged() -> None:
    """The wrapper passes its operation's result through without rewriting it."""
    expected = ("stdout text", "stderr text")

    async def exercise() -> tuple[str, str]:
        """Run one successful teardown and return what the wrapper produced."""

        async def operation() -> tuple[str, str]:
            """Return the captured text this drain would report."""
            await asyncio.sleep(0)
            return expected

        return await coordinator._run_line_stream_teardown(
            _teardown_run([]),
            operation(),
        )

    assert asyncio.run(exercise()) == expected, (
        "the teardown wrapper must return the operation's result unchanged"
    )


def test_teardown_wrapper_reports_no_completion_when_cleanup_fails() -> None:
    """A failed teardown emits the started boundary only, and propagates."""
    failure = ValueError("teardown failed")
    order: list[object] = []

    async def exercise() -> None:
        """Run the wrapper over a failing operation."""

        async def operation() -> None:
            """Fail the teardown after it has begun."""
            await asyncio.sleep(0)
            raise failure

        await coordinator._run_line_stream_teardown(
            _teardown_run(order),
            operation(),
        )

    with pytest.raises(ValueError, match="teardown failed") as caught:
        asyncio.run(exercise())

    assert caught.value is failure, (
        f"a failed teardown must propagate its own error, got {caught.value!r}"
    )
    assert order == [LineStreamPhase.TEARDOWN_STARTED], (
        f"a failed teardown must not report completion, got {order!r}"
    )
