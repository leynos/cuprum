"""The handle's own bookkeeping, driven through process doubles.

The behaviour tests in ``tests/behaviour/test_running_command.py`` drive real
children, which is what makes them evidence about signal return codes and
reaping. This module covers what a real child cannot arrange on demand: a
process that is already gone before a signal is attempted, a platform with no
route to a caller-named signal, and a cancellation timed to land inside the
grace window of a teardown. Each of those is a branch a caller depends on and
none can be reached by asking a real child nicely.

The doubles are deliberately thin. They record what was delivered and expose a
``returncode`` and a ``wait()``, which is the whole surface the handle's
control and settlement paths read; nothing here re-implements a subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
import typing as typ

import pytest

from cuprum._execution_tracking import _ExecutionTracking
from cuprum._pipeline_types import _ExecutionHooks, _StageObservation
from cuprum._process_handle import RunningCommand
from cuprum._process_handle.tasks import _HandleRun
from cuprum._sink_lifecycle import _SinkBracket
from cuprum._streams import _RelayDiagnostics
from cuprum._subprocess_spawn import _SubprocessExecution
from cuprum._subprocess_wait import _RunTaskOwnership
from cuprum.sh import (
    ExecutionContext,
    SignalAfterExitError,
    UnsupportedSignalError,
)
from tests.helpers.catalogue import python_builder

#: Grace a teardown is given before it escalates. Short, because the
#: cancellation is timed against the signal landing rather than the clock.
_ESCALATION_GRACE = 0.3

#: Status a graceful stop leaves behind, and the one an escalation leaves.
_TERMINATED = -signal.SIGTERM
_KILLED = -signal.SIGKILL

#: Status the already-exited double chose for itself.
_EARLY_EXIT = 3

#: How many waiters the single-settlement case issues.
_WAITERS = 5


class _StubProcess:
    """Process double that records what was delivered and when it ended.

    ``terminate`` and ``kill`` end the double, each recording the status the
    matching real signal would produce. That is what makes a graceful stop
    distinguishable from an escalation here as well as in the behaviour tests:
    a handle that escalated without being asked would report ``-9`` where this
    double was only terminated, and the assertion would see it.
    """

    def __init__(self, *, pid: int = 4321) -> None:
        """Start unexited, recording nothing delivered."""
        self.pid = pid
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.signals: list[int] = []
        self.exited = asyncio.Event()

    def terminate(self) -> None:
        """Deliver ``SIGTERM``, which this double takes the default way."""
        self.terminate_calls += 1
        self.exit_with(_TERMINATED)

    def kill(self) -> None:
        """Deliver ``SIGKILL``, which no child can refuse."""
        self.kill_calls += 1
        self.exit_with(_KILLED)

    def send_signal(self, signal_number: int) -> None:
        """Record a caller-named signal without ending the double."""
        self.signals.append(signal_number)

    async def wait(self) -> int:
        """Block until the double exits, then report how it ended."""
        await self.exited.wait()
        return self.returncode if self.returncode is not None else -1

    def exit_with(self, code: int) -> None:
        """End the double with *code*, keeping the first code recorded."""
        if self.returncode is None:
            self.returncode = code
        self.exited.set()


class _SigtermImmuneProcess(_StubProcess):
    """Process double that ignores ``terminate()``; only ``kill()`` ends it.

    A real ``SIGTERM``-immune child is what makes the escalation window
    observable, so a teardown interrupted mid-grace is a real risk rather than
    a hypothetical one. The double announces the signal as it ignores it, which
    is how a case times its cancellation against the *opening* of the window
    instead of against a sleep.
    """

    def __init__(self, *, pid: int = 4322) -> None:
        """Start unexited, with the signal announcement unset."""
        super().__init__(pid=pid)
        self.signalled = asyncio.Event()

    def terminate(self) -> None:
        """Record the signal and announce that the grace window has opened."""
        self.terminate_calls += 1
        self.signalled.set()


async def _consumer(text: str | None) -> str | None:
    """Stand in for a stream consumer that has already drained its pipe.

    It yields once before returning, because a consumer that never suspends
    would be complete before the settlement could ever observe it pending —
    and a run whose readers had already finished would exercise a different
    drain than the one these cases are about.

    Returns
    -------
    str | None
        The text the real consumer would have captured.
    """
    await asyncio.sleep(0)
    return text


def _stub_run(process: _StubProcess, *, grace: float) -> _HandleRun:
    """Assemble the record a handle is driven through around *process*.

    Every field is real except the process: the tracking, the bracket, and the
    observation are the same objects ``SafeCmd.start()`` builds, so the paths
    under test are the production ones and only the child is a double.

    Returns
    -------
    _HandleRun
        The owned record the handle's methods read.
    """
    command = python_builder()("-c", "pass")
    observation = _StageObservation(
        cmd=command,
        hooks=_ExecutionHooks(before_hooks=(), after_hooks=(), observe_hooks=()),
        tags={},
        cwd=None,
        env_overlay=None,
        pending_tasks=[],
        wall_clock=time.monotonic,
    )
    execution = _SubprocessExecution(
        cmd=command,
        ctx=ExecutionContext(cancel_grace=grace),
        capture=True,
        echo_stdout=False,
        echo_stderr=False,
        max_echo_line_bytes=None,
        sink_session=None,
        timeout=None,
        observation=observation,
        stdin_data=None,
    )
    ownership = _RunTaskOwnership(
        stdin_task=None,
        consumers=(
            asyncio.ensure_future(_consumer("")),
            asyncio.ensure_future(_consumer("")),
        ),
        discard_on_cancel=asyncio.Event(),
        relay_diagnostics=(_RelayDiagnostics(), _RelayDiagnostics()),
    )
    started_at = time.perf_counter()
    return _HandleRun(
        process=typ.cast("asyncio.subprocess.Process", process),
        execution=execution,
        started_at=started_at,
        wall_clock_started_at=time.time(),
        tracking=_ExecutionTracking(
            execution_hooks=_ExecutionHooks((), (), ()),
            pending_tasks=[],
            sink_bracket=_SinkBracket(None),
        ),
        ownership=ownership,
    )


def _handle(
    process: _StubProcess, *, grace: float = _ESCALATION_GRACE
) -> RunningCommand:
    """Return a handle owning *process*, ready to be driven."""
    return RunningCommand(_stub_run(process, grace=grace))


def test_repeated_termination_reports_the_one_settlement() -> None:
    """A second ``terminate()`` reports the recorded outcome and sends nothing.

    The child is stopped once. A second stop is a no-op that hands back the
    same ``CommandResult``, so a caller coordinating a stop from two places
    cannot reap the child twice or build a second result that disagrees with
    the first.
    """

    async def run_case() -> None:
        """Terminate twice and compare what each call reported."""
        process = _StubProcess()
        handle = _handle(process)

        first = await handle.terminate()
        second = await handle.terminate()

        assert first is second, (
            "a repeated terminate must return the recorded outcome, not build "
            "a second one"
        )
        assert process.terminate_calls == 1, (
            f"SIGTERM must be delivered once, got {process.terminate_calls}"
        )
        assert process.kill_calls == 0, (
            "a child that died on SIGTERM must not be escalated"
        )
        assert first.exit_code == _TERMINATED, (
            f"a graceful stop must report {_TERMINATED}, got {first.exit_code}"
        )

    asyncio.run(run_case())


def test_repeated_kill_reports_the_one_settlement() -> None:
    """A second ``kill()`` reports the recorded outcome and delivers nothing.

    ``kill`` takes the forced path rather than escalating with a zero grace,
    so the status the caller sees is the one they asked for rather than
    whichever signal happened to end the child.
    """

    async def run_case() -> None:
        """Kill twice and compare what each call reported."""
        process = _StubProcess()
        handle = _handle(process)

        first = await handle.kill()
        second = await handle.kill()

        assert first is second, "a repeated kill must return the recorded outcome"
        assert process.kill_calls == 1, (
            f"SIGKILL must be delivered once, got {process.kill_calls}"
        )
        assert process.terminate_calls == 0, (
            "kill must compel the child rather than ask it first"
        )
        assert first.exit_code == _KILLED, (
            f"a forced stop must report {_KILLED}, got {first.exit_code}"
        )

    asyncio.run(run_case())


def test_concurrent_waits_share_one_settlement() -> None:
    """Several waiters observe the one settlement, none building its own.

    Settlement is a task the handle owns rather than a lock held across the
    wait, which is what lets the waiters share it: the first creates the task,
    the rest find it, and all of them read the same result object.
    """

    async def run_case() -> None:
        """Issue every wait before the child exits, then release it."""
        process = _StubProcess()
        handle = _handle(process)
        waiters = [asyncio.ensure_future(handle.wait()) for _ in range(_WAITERS)]
        # Let every waiter reach the shared settlement before the child exits,
        # so the case exercises concurrent readers rather than a cached result.
        for _ in range(_WAITERS):
            await asyncio.sleep(0)
        assert all(not waiter.done() for waiter in waiters), (
            "every waiter must be pending when the child is still running"
        )
        assert handle.result is None, "a running child has no result to report"

        process.exit_with(0)
        results = await asyncio.gather(*waiters)

        first = results[0]
        assert all(result is first for result in results), (
            "every concurrent wait must observe the one settlement"
        )
        assert first.exit_code == 0, "the shared outcome is the child's own"
        assert handle.result is first, "the settlement publishes what wait returned"

    asyncio.run(run_case())


def test_signal_after_exit_names_the_recorded_code() -> None:
    """Signalling an exited child reports how it ended rather than a bare race.

    A caller coordinating several signals cannot avoid racing the child's own
    exit, so the refusal carries the exit code the handle recorded and the PID
    it belonged to, and nothing is delivered.
    """

    async def run_case() -> None:
        """Attempt a signal after the child has already exited."""
        process = _StubProcess(pid=4323)
        process.exit_with(_EARLY_EXIT)
        handle = _handle(process)

        with pytest.raises(SignalAfterExitError) as raised:
            await handle.signal(signal.SIGTERM)

        assert raised.value.exit_code == _EARLY_EXIT, (
            f"the refusal must carry the child's status {_EARLY_EXIT}, "
            f"got {raised.value.exit_code}"
        )
        assert raised.value.pid == process.pid, (
            "the refusal must name the process it could not reach"
        )
        assert not process.signals, "nothing may be delivered to a gone child"

    asyncio.run(run_case())


def test_signal_is_refused_when_platform_cannot_deliver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform with no route to the signal is named, and nothing is sent.

    The refusal carries both facts a caller needs to choose what to do next —
    the signal that was asked for and the platform that declined — so they
    never have to re-read the handle to find out why it was refused.
    """

    async def run_case() -> None:
        """Attempt a signal the platform cannot deliver."""
        process = _StubProcess(pid=4324)
        handle = _handle(process)
        monkeypatch.setattr(
            "cuprum._process_handle.control._arbitrary_signals_supported",
            _platform_cannot_deliver,
        )

        with pytest.raises(UnsupportedSignalError) as raised:
            await handle.signal(signal.SIGTERM)

        assert raised.value.signal_number == signal.SIGTERM, (
            "the refusal must name the signal that was asked for"
        )
        assert raised.value.platform_name, (
            "the refusal must name the platform that declined"
        )
        assert not process.signals, "an undeliverable signal must not be sent"
        assert process.returncode is None, "the child must be left running"

    asyncio.run(run_case())


def test_platform_refusal_is_reported_ahead_of_the_childs_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gone child on a restricted platform reports the platform, not the race.

    Both refusals describe a signal that was not delivered, but only one of
    them is actionable: a caller told the child had exited gives up, while a
    caller told the platform cannot take the request knows to reach for
    ``terminate`` or ``kill`` instead. The platform is therefore checked first,
    and this case is what holds that ordering in place — with a child that has
    already exited, a swap of the two checks would silently report the race.
    """

    async def run_case() -> None:
        """Attempt a signal where both refusals apply."""
        process = _StubProcess(pid=4325)
        process.exit_with(_EARLY_EXIT)
        handle = _handle(process)
        monkeypatch.setattr(
            "cuprum._process_handle.control._arbitrary_signals_supported",
            _platform_cannot_deliver,
        )

        with pytest.raises(UnsupportedSignalError) as raised:
            await handle.signal(signal.SIGTERM)

        assert raised.value.signal_number == signal.SIGTERM, (
            "the platform's refusal must be the one reported"
        )

    asyncio.run(run_case())


def _platform_cannot_deliver() -> bool:
    """Report that this platform cannot deliver a caller-named signal."""
    return False


def test_teardown_cancelled_mid_grace_still_settles() -> None:
    """A cancellation inside teardown reaps the child *and* records its outcome.

    Stopping and settling are separate steps, and a cancellation landing on the
    first must not skip the second. Reaping without settling is half-ownership:
    the child is gone, but the handle's ``result`` stays ``None`` forever and
    every later caller is told nothing about a child that has already been
    dealt with.

    The child ignores ``SIGTERM``, so the cancellation lands inside the grace
    window while the escalation is still pending. Cancelling there is what
    distinguishes the two behaviours: had the shield not held, the escalation
    would be skipped and a ``SIGTERM``-immune child would outlive its handle.
    """

    async def run_case() -> None:
        """Cancel the block leaving the handle, then inspect what survived."""
        process = _SigtermImmuneProcess()
        handle = _handle(process, grace=5.0)

        async def leave_context() -> None:
            """Enter the handle's context and leave it at once."""
            async with handle:
                await asyncio.sleep(0)

        task = asyncio.ensure_future(leave_context())
        await process.signalled.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert process.kill_calls == 1, (
            "the escalation must complete before the cancellation propagates, "
            "otherwise a SIGTERM-immune child outlives the handle"
        )
        assert process.returncode == _KILLED, (
            f"the child must be reaped by the escalation, got {process.returncode}"
        )
        result = handle.result
        assert result is not None, (
            "a cancellation during teardown must not leave the handle unsettled"
        )
        assert result.exit_code == _KILLED, (
            "the recorded outcome must report the signal that ended the child, "
            f"got {result.exit_code}"
        )
        assert task.cancelled(), (
            "teardown's failure is the caller's to see when the block succeeded"
        )

    asyncio.run(run_case())


def test_teardown_failure_does_not_replace_the_blocks_error() -> None:
    """A block that fails keeps its own error, whatever teardown did.

    Nothing here suppresses: the error that caused the exit is the one the
    caller has to see, so a teardown failure is left on the observation stream
    rather than replacing it. Settlement still happens, because the handle
    that recorded no result would be the harder problem to diagnose.
    """

    async def run_case() -> None:
        """Fail the block while teardown stops the child."""
        process = _SigtermImmuneProcess()
        handle = _handle(process, grace=0.05)
        raised: BaseException | None = None
        try:
            async with handle:
                _raise_block_error()
        except ValueError as failure:
            # Carried out rather than handled: which error reaches the caller
            # is exactly what this case asserts.
            raised = failure

        assert raised is not None, "the block's own error must reach the caller"
        assert str(raised) == "block error", (
            "the block's message must survive teardown intact"
        )
        assert handle.result is not None, (
            "teardown must settle the handle even while the block fails"
        )

    asyncio.run(run_case())


def _raise_block_error() -> None:
    """Raise the error a block fails with.

    Raised from a helper so the ``raise`` does not sit inside the ``try`` whose
    handler the case is testing.

    Raises
    ------
    ValueError
        Always, carrying the message the case asserts survives teardown.
    """
    msg = "block error"
    raise ValueError(msg)
