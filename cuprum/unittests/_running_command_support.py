"""Process doubles and run assembly shared by the handle's unit tests.

The handle's control and settlement paths are covered by two modules, and both
need the same one thing a real child cannot provide on demand: a process that
is already gone before a signal is attempted, a ``SIGTERM``-immune process
whose grace window can be observed, a cancellation timed to land inside that
window. This module is where those doubles live, so neither test module owns
them and neither has to import the other.

The doubles are deliberately thin. They record what was delivered and expose a
``returncode`` and a ``wait()``, which is the whole surface the handle's
control and settlement paths read; nothing here re-implements a subprocess.
:func:`_handle` assembles the real surrounding record around one of them, so
the paths under test are the production ones and only the child is a double.
"""

from __future__ import annotations

import asyncio
import signal
import time
import typing as typ

from cuprum._execution_tracking import _ExecutionTracking
from cuprum._pipeline_types import _ExecutionHooks, _StageObservation
from cuprum._process_handle import RunningCommand
from cuprum._process_handle.tasks import _HandleRun
from cuprum._sink_lifecycle import _SinkBracket
from cuprum._streams import _RelayDiagnostics
from cuprum._subprocess_spawn import _SubprocessExecution
from cuprum._subprocess_wait import _RunTaskOwnership
from cuprum.sh import ExecutionContext
from tests.helpers.catalogue import python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult, SafeCmd

__all__ = [
    "_EARLY_EXIT",
    "_ESCALATION_GRACE",
    "_KILLED",
    "_TERMINATED",
    "_WAITERS",
    "_ConcurrentlyReapedProcess",
    "_SigtermImmuneProcess",
    "_StubProcess",
    "_StubRunConfig",
    "_handle",
]

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


class _ConcurrentlyReapedProcess(_StubProcess):
    """Process double whose child is reaped and published during delivery.

    This models the one sequence the probe alone cannot cover. The handle
    reads the child's state and sees nothing — the status has not been
    published yet — so it attempts delivery. A reaper collecting the child
    concurrently is what makes that delivery fail: the kernel no longer has
    the process, and once the status is collected the non-reaping probe cannot
    observe the ending either. The exit code exists only in the value the
    collecting reaper publishes.

    The two moments are ordered as they really happen rather than raced for:
    the double is unexited when the handle reads it, and the reap lands inside
    the delivery attempt. That ordering is what makes the case deterministic —
    a real race here would be winnable in one direction only by luck, and the
    behaviour under test is the handle's reporting, not the scheduler's
    choices.
    """

    def __init__(self, *, exit_code: int, pid: int = 4325) -> None:
        """Start with the child still unexited and nothing published."""
        super().__init__(pid=pid)
        self._reaped_under = exit_code

    def send_signal(self, signal_number: int) -> None:
        """Record the attempt, publish the concurrent reap, refuse delivery.

        The parent's recording runs first, so ``signals`` carries the number
        the caller asked to deliver. That is what witnesses the attempt: a
        handle that short-circuited on a probe would leave the list empty, and
        the case would appear to pass while never reaching the refusal it
        exists to cover. The kernel refusing the signal is why the list
        records intent rather than delivery — the child was already collected,
        so nothing landed.

        Raises
        ------
        ProcessLookupError
            Always, standing in for the kernel's refusal to deliver to a child
            it no longer has. The refusal *is* the scenario this double exists
            to reproduce, so it is unconditional.
        """
        super().send_signal(signal_number)
        # The reaper finished between the handle's read and this call, so the
        # status is published before the delivery discovers the process gone.
        self.returncode = self._reaped_under
        self.exited.set()
        msg = f"No such process (PID {self.pid})"
        raise ProcessLookupError(msg)


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


async def _consumer(text: str | None, delay: float = 0.0) -> str | None:
    """Stand in for a stream consumer that has already drained its pipe.

    It yields once before returning, because a consumer that never suspends
    would be complete before the settlement could ever observe it pending —
    and a run whose readers had already finished would exercise a different
    drain than the one these cases are about. A caller that needs the drain to
    still be running when it acts passes *delay*, which holds the consumers
    open past the capture EOF grace the drain allows them.

    Returns
    -------
    str | None
        The text the real consumer would have captured.
    """
    await asyncio.sleep(max(0, delay))
    return text


class _StubRunConfig(typ.NamedTuple):
    """What a case varies about the run behind a stubbed handle.

    Every field is a straight pass-through to the record the handle is driven
    through: the grace its stop allows, the deadline ``start()`` would have
    resolved, the after-hooks its settlement fires, and how long its stubbed
    consumers stay open past the capture EOF grace. They are one record rather
    than four keyword arguments because they describe one thing — how the
    stubbed run behaves — so a case that varies one is configuring that run
    rather than choosing among unrelated knobs. The defaults are the shape a
    case gets when it is not varying anything.
    """

    grace: float = _ESCALATION_GRACE
    default_timeout: float | None = None
    after_hooks: tuple[cabc.Callable[[SafeCmd, CommandResult], None], ...] = ()
    drain_delay: float = 0.0


#: The configuration a case leaves alone unless it is varying something.
_UNVARIED_RUN = _StubRunConfig()


def _handle(
    process: _StubProcess,
    config: _StubRunConfig = _UNVARIED_RUN,
) -> RunningCommand:
    """Return a handle owning *process*, ready to be driven.

    Every field of the assembled record is real except the process: the
    tracking, the bracket, and the observation are the same objects
    ``SafeCmd.start()`` builds, so the paths under test are the production ones
    and only the child is a double.

    Returns
    -------
    RunningCommand
        The handle, owning *process* and driven entirely by the production
        control and settlement paths.
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
        ctx=ExecutionContext(cancel_grace=config.grace),
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
            asyncio.ensure_future(_consumer("", config.drain_delay)),
            asyncio.ensure_future(_consumer("", config.drain_delay)),
        ),
        discard_on_cancel=asyncio.Event(),
        relay_diagnostics=(_RelayDiagnostics(), _RelayDiagnostics()),
    )
    started_at = time.perf_counter()
    run = _HandleRun(
        process=typ.cast("asyncio.subprocess.Process", process),
        execution=execution,
        started_at=started_at,
        wall_clock_started_at=time.time(),
        tracking=_ExecutionTracking(
            execution_hooks=_ExecutionHooks((), config.after_hooks, ()),
            pending_tasks=[],
            sink_bracket=_SinkBracket(None),
        ),
        ownership=ownership,
        default_timeout=config.default_timeout,
    )
    return RunningCommand(run)
