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
:func:`_stub_run` assembles the real surrounding record around one of them, so
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
    "_SigtermImmuneProcess",
    "_StubProcess",
    "_handle",
    "_stub_run",
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


def _stub_run(
    process: _StubProcess,
    *,
    grace: float,
    default_timeout: float | None = None,
    after_hooks: tuple[cabc.Callable[[SafeCmd, CommandResult], None], ...] = (),
    drain_delay: float = 0.0,
) -> _HandleRun:
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
            asyncio.ensure_future(_consumer("", drain_delay)),
            asyncio.ensure_future(_consumer("", drain_delay)),
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
            execution_hooks=_ExecutionHooks((), after_hooks, ()),
            pending_tasks=[],
            sink_bracket=_SinkBracket(None),
        ),
        ownership=ownership,
        default_timeout=default_timeout,
    )


def _handle(
    process: _StubProcess,
    *,
    grace: float = _ESCALATION_GRACE,
    default_timeout: float | None = None,
    after_hooks: tuple[cabc.Callable[[SafeCmd, CommandResult], None], ...] = (),
    drain_delay: float = 0.0,
) -> RunningCommand:
    """Return a handle owning *process*, ready to be driven."""
    return RunningCommand(
        _stub_run(
            process,
            grace=grace,
            default_timeout=default_timeout,
            after_hooks=after_hooks,
            drain_delay=drain_delay,
        )
    )
