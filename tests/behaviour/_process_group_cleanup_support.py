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
import time
import typing as typ

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions
from tests.behaviour._process_group_cancellation import _cancel_run, _Cancellation
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
    pid_file:
        Where the grandchild records its pid, which is also the readiness
        signal that it is running and immune.
    events:
        The run's observe stream, retained for its ``start`` event.
    """

    task: asyncio.Task[object]
    control: asyncio.subprocess.Process
    pid_file: Path
    events: list[ExecEvent]


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


def _pipe_holding_command(
    pid_file: Path,
) -> tuple[SafeCmd, ProgramCatalogue]:
    """Return a command whose child leaves a pipe-holding grandchild behind.

    Returns
    -------
    tuple[SafeCmd, ProgramCatalogue]
        The command, and the catalogue whose allowlist admits it.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    return python(*pipe_holding_child_argv(pid_file)), catalogue


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
        unrelated_pid=resources.control.pid,
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
    # Ordinary failures only: an interrupt or cancellation targeting the
    # scenario is not something this helper gets to absorb.
    with contextlib.suppress(Exception):
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
    command, catalogue = _pipe_holding_command(pid_file)
    control = await _spawn_control()
    events: list[ExecEvent] = []
    grandchild: int | None = None
    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(events.append):
        resources = _RunResources(
            task=_run_task(command, policy),
            control=control,
            pid_file=pid_file,
            events=events,
        )
        try:
            grandchild = await _await_grandchild(pid_file)
            cancellation = await _cancel_run(
                resources.task,
                grandchild,
                cancellations,
                SETTLE_BOUND_S,
            )
            return _describe(resources, grandchild, cancellation)
        finally:
            with contextlib.suppress(Exception):
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
