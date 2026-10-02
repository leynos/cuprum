"""Scenario scaffolding for opt-in process-group cleanup.

Every scenario has the same shape and differs only in the ownership policy it
runs under and how often it cancels, so this module owns that shape: start a
run whose direct child leaves a ``SIGTERM``-immune grandchild holding the run's
pipes, cancel it, and report what survived. The assertions live beside the
steps that carry them; this module only reports.

The grandchild is a real process rather than a double because the guarantee
under test is a kernel-level one — whether a signal reaches beyond the direct
child. Cleanup here kills by pid and never by group, because the inherited
policy leaves the grandchild inside the *test runner's* own process group; a
``killpg`` aimed at that leftover would end the test session instead. That the
runner survives the inherited scenario is itself evidence, and the strongest
kind available: a teardown signalling a group it does not own would end the
scenario by ending the process running it.

The outcome is described *before* any cleanup runs. Cleanup kills the
descendant, so a description taken afterwards would report the harness's work
rather than the run's — the inherited scenario in particular would silently
lose the very fact it exists to assert.

The whole scenario runs inside one event loop. A run is cancelled through its
own task, and a task belongs to the loop that made it, so starting it in one
step and cancelling it in another would need that loop to outlive the step.
Composing the scenario as a single coroutine removes the question.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import os
import signal
import threading
import time
import typing as typ

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import (
    pending_tasks,
    pipe_holding_child_argv,
    process_is_running,
    python_interpreter,
    started_pids,
    wait_for_pid_file,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecEvent
    from cuprum.sh import SafeCmd

# Long enough for the SIGTERM-then-SIGKILL escalation to play out inside the
# scenario's patience, short enough that a run ignoring its grace period is
# noticed rather than waited out.
_GRACE_SECONDS = 0.2
# The grandchild records its pid only after installing its handler, so waiting
# for that file is waiting for a genuinely immune descendant rather than for an
# interval guessed at.
_READY_SECONDS = 10.0
# A repeated cancellation has to land *inside* the teardown the first one
# started. The grace period is what keeps the run in flight that long, so this
# interval samples it rather than racing the whole teardown.
_CANCEL_INTERVAL_S = 0.05
_CONTROL_SLEEP_S = 300
_REAP_SECONDS = 5.0
# The bound a settled run falls inside: the grace period, the reap that follows
# it, and room for a scheduling hiccup. A run parked on a pipe its descendant
# still held would blow straight through it.
SETTLE_BOUND_S = 3.0

_POLICIES = {
    "ownership": ProcessGroupPolicy.OWN_GROUP,
    "inherited": ProcessGroupPolicy.INHERIT,
}


@dc.dataclass(frozen=True, slots=True)
class CleanupOutcome:
    """What one cancelled run left behind.

    Attributes
    ----------
    direct_child_pid:
        The run's own child, taken from the ``start`` event the run emitted.
    grandchild_pid:
        The descendant that child left behind, holding the run's pipes.
    unrelated_pid:
        A process started alongside the run that belongs to no group it owns.
    direct_child_alive, grandchild_alive, unrelated_alive:
        Whether each process still existed once the run had settled.
    pending:
        Tasks the run left unfinished on the loop after settling.
    cancellations:
        How many cancellations found the run still running.
    settle_seconds:
        Wall-clock time from the first cancellation to the run settling.
    raised:
        How the run ended: the exception type's name, or ``"returned"``.
    rescued:
        Whether the run's teardown had to be unblocked rather than settling on
        its own. Assertions refuse such an outcome: the descendant is gone, but
        the harness removed it, so the run reclaimed nothing.
    """

    direct_child_pid: int
    grandchild_pid: int
    unrelated_pid: int
    direct_child_alive: bool
    grandchild_alive: bool
    unrelated_alive: bool
    pending: int
    cancellations: int
    settle_seconds: float
    raised: str
    rescued: bool


@dc.dataclass(frozen=True, slots=True)
class _RunResources:
    """The live resources one scenario has to account for.

    Attributes
    ----------
    task:
        The run under test, cancelled by the scenario.
    control:
        A process belonging to no group the run owns.
    pid_file, marker:
        Where the grandchild records its pid, and where it signals readiness.
    events:
        The run's observe stream, retained for its ``start`` event.
    """

    task: asyncio.Task[object]
    control: asyncio.subprocess.Process
    pid_file: Path
    marker: Path
    events: list[ExecEvent]


@dc.dataclass(frozen=True, slots=True)
class _Cancellation:
    """How a run responded to being cancelled.

    Attributes
    ----------
    landings:
        How many cancellations found the run still running.
    raised:
        The exception the run ended on, or ``"returned"``.
    elapsed:
        Wall-clock seconds from the first cancellation to the run settling.
    rescued:
        Whether teardown had to be unblocked.
    """

    landings: int
    raised: str
    elapsed: float
    rescued: bool


def policy_for(name: str) -> ProcessGroupPolicy:
    """Return the ownership policy a scenario's name refers to.

    Returns
    -------
    ProcessGroupPolicy
        ``OWN_GROUP`` for ``"ownership"``, ``INHERIT`` for ``"inherited"``.
    """
    return _POLICIES[name]


def _control_argv() -> tuple[str, str, str]:
    """Return ``-c`` argv for a process that belongs to no run under test."""
    return ("-c", f"import time; time.sleep({_CONTROL_SLEEP_S})", "")


class _RescueWatchdog:
    """Force a stuck run to settle, in a thread of its own.

    A run's teardown is cancellation-safe by design: ``_shielded_cleanup``
    absorbs every cancellation until its cleanup finishes, so an *in*-loop
    timeout cannot interrupt a teardown that is stuck. Cancelling harder would
    only repeat a request the loop has already declined to honour.

    Killing the descendant the teardown is waiting on is therefore the one
    thing that can unblock it, and a thread is what can do that while the loop
    is busy. That a rescue was needed is recorded rather than hidden; the
    scenario's assertions refuse the outcome on that basis.
    """

    def __init__(self, pid: int, grace_seconds: float) -> None:
        """Start watching *pid*, arming a kill for after the grace period."""
        self._pid = pid
        self._timer = threading.Timer(grace_seconds, self._rescue)
        self.rescued = False

    def _rescue(self) -> None:
        """End the process the teardown is waiting on, recording that it stuck."""
        self.rescued = True
        # Never a group: the descendant's own group is what the stuck teardown
        # failed to signal, and the run's group is the only one in scope.
        with contextlib.suppress(ProcessLookupError):
            os.kill(self._pid, signal.SIGKILL)

    def __enter__(self) -> _RescueWatchdog:
        """Arm the watchdog."""
        self._timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Disarm the watchdog, so a settled run arms nothing."""
        self._timer.cancel()


async def _cancel_run(
    task: asyncio.Task[object],
    grandchild: int,
    cancellations: int,
) -> _Cancellation:
    """Cancel the run *cancellations* times and report how it responded."""
    landed = 0
    cancelled_at = time.monotonic()
    with _RescueWatchdog(grandchild, SETTLE_BOUND_S) as watchdog:
        for _ in range(cancellations):
            # A cancellation arriving after the run has settled proves nothing
            # about interrupted cleanup, so it is not counted as one that did.
            if task.done():
                break
            task.cancel()
            landed += 1
            # Yield between cancellations so a repeat arrives while the
            # shielded teardown is still working, rather than coalescing onto
            # the request the first one already made.
            await asyncio.sleep(_CANCEL_INTERVAL_S)
        try:
            # Bounded for the benefit of the cancellation itself, not as a
            # guard on a stuck teardown: the shield absorbs this timeout the
            # same way it absorbs any other cancellation. The watchdog is what
            # covers that case.
            async with asyncio.timeout(SETTLE_BOUND_S):
                await task
        except asyncio.CancelledError:
            raised = "CancelledError"
        except TimeoutError:
            raised = "TimeoutError"
        except BaseException as exc:  # ruff: ignore[blind-except] - report what it saw
            raised = type(exc).__name__
        else:
            raised = "returned"
    return _Cancellation(
        landings=landed,
        raised=raised,
        elapsed=time.monotonic() - cancelled_at,
        rescued=watchdog.rescued,
    )


def _pipe_holding_command(
    pid_file: Path,
    marker: Path,
) -> tuple[SafeCmd, ProgramCatalogue]:
    """Return a command whose child leaves a pipe-holding grandchild behind.

    Returns
    -------
    tuple[SafeCmd, ProgramCatalogue]
        The command, and the catalogue whose allowlist admits it.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    return python(*pipe_holding_child_argv(pid_file, marker)), catalogue


def _run_task(command: SafeCmd, policy: ProcessGroupPolicy) -> asyncio.Task[object]:
    """Start the run under *policy*, without waiting for it to get going."""
    return asyncio.ensure_future(
        command.run(
            output=RunOutputOptions(capture=True, echo=False),
            context=ExecutionContext(
                cancel_grace=_GRACE_SECONDS,
                process_group=policy,
            ),
        )
    )


async def _spawn_control() -> asyncio.subprocess.Process:
    """Start a process belonging to no group any run owns."""
    return await asyncio.create_subprocess_exec(
        python_interpreter(),
        *_control_argv(),
    )


def _describe(
    resources: _RunResources,
    grandchild: int,
    cancellation: _Cancellation,
) -> CleanupOutcome:
    """Describe what the run left behind, before cleanup changes any of it."""
    (direct_child_pid,) = started_pids(resources.events)
    return CleanupOutcome(
        direct_child_pid=direct_child_pid,
        grandchild_pid=grandchild,
        unrelated_pid=typ.cast("int", resources.control.pid),
        direct_child_alive=process_is_running(direct_child_pid),
        grandchild_alive=process_is_running(grandchild),
        unrelated_alive=process_is_running(resources.control.pid),
        pending=len(pending_tasks()),
        cancellations=cancellation.landings,
        settle_seconds=cancellation.elapsed,
        raised=cancellation.raised,
        rescued=cancellation.rescued,
    )


async def _await_grandchild(pid_file: Path) -> int:
    """Return the grandchild's pid once it is running and immune.

    Returns
    -------
    int
        The pid the grandchild recorded.
    """
    # The pid file is written after the handler is installed, so waiting for it
    # is waiting for a descendant that is genuinely immune — and for a run that
    # has really started one.
    return await asyncio.to_thread(
        wait_for_pid_file,
        pid_file,
        seconds=_READY_SECONDS,
        context="grandchild readiness",
    )


def _reap_pid(pid: int) -> None:
    """End a leftover descendant, never signalling a process group.

    The pid-only route is what makes this safe under both policies. The owned
    run's group is already empty by the time this runs, but the inherited
    run's grandchild shares the test runner's group, where ``killpg`` would
    take the session down with the leftover.
    """
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + _REAP_SECONDS
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            return
        time.sleep(0.05)


async def _end_process(process: asyncio.subprocess.Process) -> None:
    """Collect a process the scenario started, whether or not it is still up."""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def _clean_up(resources: _RunResources, grandchild: int | None) -> None:
    """Leave no process behind, whatever became of the scenario.

    The order matters. A run still polling its group is only unblocked once the
    descendant holding that group open is reaped, so the reap comes before the
    wait for the run. Each step is guarded so cleanup cannot replace the
    outcome the scenario is reporting, nor raise where the body already is.
    """
    if not resources.task.done():
        resources.task.cancel()
    if grandchild is not None and process_is_running(grandchild):
        await asyncio.to_thread(_reap_pid, grandchild)
    # Bounded, because a run wedged on a descendant that will not die cannot be
    # made to settle from here, and awaiting it endlessly would report that as
    # a killed session rather than as this scenario's failure.
    await asyncio.wait({resources.task}, timeout=_REAP_SECONDS)
    with contextlib.suppress(BaseException):
        await _end_process(resources.control)


async def _drive_scenario(
    workdir: Path,
    policy: ProcessGroupPolicy,
    cancellations: int,
) -> CleanupOutcome:
    """Start the run, cancel it, and describe what it left behind.

    The observe hook is entered here rather than where the run is created: the
    run is only *scheduled* at that point, and it emits its ``start`` event
    once the loop first runs it, by which time a scope opened around the
    ``ensure_future`` call would already have closed. Holding the scope across
    the whole scenario is what makes the pid available.

    Returns
    -------
    CleanupOutcome
        The pids involved and how each of them fared once the run settled.
    """
    pid_file = workdir / "grandchild.pid"
    marker = workdir / "grandchild.ready"
    command, catalogue = _pipe_holding_command(pid_file, marker)
    control = await _spawn_control()
    events: list[ExecEvent] = []
    grandchild: int | None = None
    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(events.append):
        resources = _RunResources(
            task=_run_task(command, policy),
            control=control,
            pid_file=pid_file,
            marker=marker,
            events=events,
        )
        try:
            grandchild = await _await_grandchild(pid_file)
            cancellation = await _cancel_run(resources.task, grandchild, cancellations)
            return _describe(resources, grandchild, cancellation)
        finally:
            with contextlib.suppress(BaseException):
                await _clean_up(resources, grandchild)


def run_scenario(
    workdir: Path,
    policy: ProcessGroupPolicy,
    cancellations: int,
) -> CleanupOutcome:
    """Run one cleanup scenario to completion and report its outcome.

    Returns
    -------
    CleanupOutcome
        The pids involved and how each of them fared once the run settled.
    """
    return asyncio.run(_drive_scenario(workdir, policy, cancellations))
