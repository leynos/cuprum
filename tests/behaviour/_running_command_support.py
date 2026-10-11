"""Child builders and start/wait orchestration for the handle scenarios.

The companion module :mod:`tests.behaviour._running_command_control_support`
drives the stop, signal, and teardown scenarios. Both are imported by
:mod:`tests.behaviour.test_running_command`, which owns the assertions, and
both carry a leading underscore so pytest does not collect them as test
modules.

Each ``_observe_*`` function runs one scenario's whole story inside a single
event loop and returns a :class:`HandleObservation`. That shape is forced by
the artefact under test rather than chosen for convenience: a
``RunningCommand`` holds an :class:`asyncio.Lock` and an asyncio subprocess,
both bound to the loop that spawned them, so a handle cannot be carried across
two ``asyncio.run`` calls. A scenario spanning several steps would have to
re-open a loop mid-story, which is exactly what a handle cannot survive.

The children come from :mod:`tests.helpers.timeouts`, so these scenarios drive
the same child protocol as the existing timeout tests rather than introducing
a second one: a child that writes identifiable output on both streams, records
a readiness marker, and then blocks for several minutes.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum import sh
from cuprum.catalogue import ProgramCatalogue
from cuprum.program import Program
from cuprum.sh import ExecutionContext, RunOutputOptions, TimeoutExpired
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import (
    CHILD_STDOUT,
    child_argv,
    process_is_running,
    stubborn_child_argv,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import CommandResult, RunningCommand, SafeCmd

__all__ = [
    "HandleObservation",
    "ReadyChild",
    "_absent_start_command",
    "_delayed_writer_command",
    "_exit_code_command",
    "_observe_concurrent_waits",
    "_observe_early_exit_signal",
    "_observe_failed_start",
    "_observe_output_wait",
    "_observe_start",
    "_observe_wait_deadline",
    "_python_builder",
    "_ready_child",
]

#: Marker the delayed writer emits, so its scenario can assert the settlement
#: carries the child's output rather than merely that some text arrived.
DELAYED_WRITER_MARKER = "delayed-writer-marker"

#: Status the output-producing child exits with, distinct from both the zero a
#: clean exit reports and the negative codes a signal reports.
_OUTPUT_EXIT_CODE = 7

#: Status the early-exit child chooses for itself, so a refusal that reports
#: the wrong code cannot pass by coincidence.
_EARLY_EXIT_CODE = 3

#: How long a scenario waits for a child to announce itself before giving up.
_READY_TIMEOUT = 5.0

_EXIT_SOURCE = "import sys\nsys.exit({code})\n"
_SLEEP_SOURCE = "import time\ntime.sleep(300)\n"
_OUTPUT_EXIT_SOURCE = (
    "import sys\n"
    f"sys.stdout.write({CHILD_STDOUT!r} + chr(10))\n"
    "sys.stdout.flush()\n"
    f"sys.exit({_OUTPUT_EXIT_CODE})\n"
)
_DELAYED_WRITER_SOURCE = (
    "import time\n"
    "time.sleep(0.2)\n"
    f"print({DELAYED_WRITER_MARKER!r}, flush=True)\n"
)


@dc.dataclass(frozen=True, slots=True)
class HandleObservation:
    """The facts one handle scenario observed, for its ``Then`` steps to check.

    One record serves every scenario rather than a type per scenario. A
    scenario fills in the few fields it has something to say about and leaves
    the rest at their empty defaults, so a ``Then`` step reads a single fact
    without needing to know which scenario produced it.

    Attributes
    ----------
    result : CommandResult | None
        The outcome read from the handle after the scenario's operations, or
        ``None`` when the handle never settled one.
    results : tuple[CommandResult, ...]
        Every outcome the scenario collected, in issue order. The repeated-stop
        and concurrent-wait scenarios use this, because their whole point is
        whether several callers observed *one* outcome.
    error : BaseException | None
        The exception the scenario's operation raised, carried out rather than
        handled away: what reached the caller is the fact under test.
    handle_pid : int | None
        The PID the handle reported, as its ``start`` event announced it.
    exec_id : str
        The execution identifier the handle carried.
    child_was_live : bool
        Whether the child was still running when ``start`` returned.
    child_reaped : bool
        Whether the child had gone by the time the scenario finished. Checked
        rather than assumed, so a leaked child fails its scenario instead of
        being left behind.
    """

    result: CommandResult | None = None
    results: tuple[CommandResult, ...] = ()
    error: BaseException | None = None
    handle_pid: int | None = None
    exec_id: str = ""
    child_was_live: bool = False
    child_reaped: bool = False


@dc.dataclass(frozen=True, slots=True)
class ReadyChild:
    """A child that announces itself before blocking, plus its marker.

    The command and the marker travel together because a stop scenario needs
    both and neither is useful alone: acting before the marker exists races the
    child's own start-up and tests the wrong thing.

    Attributes
    ----------
    command : SafeCmd
        Allowlisted command that starts the child.
    marker : Path
        File the child writes once it is genuinely running.
    """

    command: SafeCmd
    marker: Path


def _python_builder() -> cabc.Callable[..., SafeCmd]:
    """Return a command builder bound to the current interpreter's catalogue.

    Returns
    -------
    cabc.Callable[..., SafeCmd]
        A builder resolving programs against the interpreter's own catalogue,
        so a scenario runs on whichever interpreter the suite is using.
    """
    catalogue, python_program = python_catalogue()
    return sh.make(python_program, catalogue=catalogue)


def _script_command(tmp_path: Path, name: str, source: str) -> SafeCmd:
    """Write *source* to *name* under *tmp_path* and wrap it as a ``SafeCmd``."""
    script_path = tmp_path / name
    script_path.write_text(source, encoding="utf-8")
    return _python_builder()(str(script_path))


def _exit_code_command(tmp_path: Path, code: int, *, writes: bool = False) -> SafeCmd:
    """Build a command that ends at once with *code*.

    Parameters
    ----------
    tmp_path : Path
        Directory the script is written under.
    code : int
        Status the child exits with.
    writes : bool, default=False
        Whether the child writes the shared stdout marker first. A scenario
        asserting capture needs output to have been produced; one asserting
        only the exit code does not, and a silent child keeps the two
        assertions independent.

    Returns
    -------
    SafeCmd
        The built command.
    """
    source = _OUTPUT_EXIT_SOURCE if writes else _EXIT_SOURCE.format(code=code)
    return _script_command(tmp_path, "exits.py", source)


def _sleep_command(tmp_path: Path) -> SafeCmd:
    """Build a command that blocks far longer than any scenario will wait."""
    return _script_command(tmp_path, "sleeper.py", _SLEEP_SOURCE)


def _delayed_writer_command(tmp_path: Path) -> SafeCmd:
    """Build a command that writes its marker only after a short delay."""
    return _script_command(tmp_path, "delayed_writer.py", _DELAYED_WRITER_SOURCE)


def _ready_child(tmp_path: Path, *, stubborn: bool) -> ReadyChild:
    """Build a child that announces readiness, then blocks until stopped.

    The two variants differ in how they meet a ``SIGTERM``, which is what makes
    a graceful stop distinguishable from an escalated one. The ordinary child
    keeps the interpreter's default disposition, so it dies of the signal and
    its outcome reports ``-SIGTERM``; the stubborn one installs ``SIG_IGN`` and
    can only be ended by the escalation, so its outcome reports ``-SIGKILL``.

    Parameters
    ----------
    tmp_path : Path
        Directory the readiness marker is written under.
    stubborn : bool
        Whether the child ignores ``SIGTERM``.

    Returns
    -------
    ReadyChild
        The command and the marker it writes once running.
    """
    marker = tmp_path / ("stubborn-ready" if stubborn else "ready")
    argv = stubborn_child_argv(marker) if stubborn else child_argv(marker)
    return ReadyChild(command=_python_builder()(*argv), marker=marker)


def _absent_start_command(tmp_path: Path) -> SafeCmd:
    """Build an allowlisted command whose absolute path does not exist.

    The program is registered in a real catalogue so the allowlist admits it;
    only the file is missing. That places the failure inside the library's own
    spawn path, which is where the handle has to take ownership of it, rather
    than at the allowlist check that runs before anything is spawned.

    Returns
    -------
    SafeCmd
        A command naming a program that is not on disk.
    """
    absent = Program(str(tmp_path / "absent-start-executable"))
    catalogue = ProgramCatalogue.from_programs(absent)
    return sh.make(absent, catalogue=catalogue)()


async def _start_owned(command: SafeCmd) -> RunningCommand:
    """Start *command* and return the handle its outcome is read through."""
    return await command.start(output=RunOutputOptions(), context=ExecutionContext())


async def _wait_for_ready(marker: Path, timeout: float = _READY_TIMEOUT) -> None:
    """Wait until the child has written its readiness *marker*.

    Polled rather than signalled because the fact being waited on is written by
    another process, which no asyncio primitive can observe. The poll is what
    makes a stop scenario deterministic: acting before the marker exists would
    race the child's own start-up.

    Raises
    ------
    TimeoutError
        If the marker has not appeared within *timeout* seconds.
    """
    if not await _poll_until(marker.exists, timeout):
        msg = f"readiness marker {marker} was not written within {timeout}s"
        raise TimeoutError(msg)


async def _poll_until(predicate: cabc.Callable[[], bool], timeout: float) -> bool:
    """Return whether *predicate* held before *timeout* elapsed.

    Returns
    -------
    bool
        Whether the predicate was satisfied, so a caller decides what a
        timeout means for its own wait.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def _reaped(pid: int | None, timeout: float = _READY_TIMEOUT) -> bool:
    """Return whether *pid* has gone, yielding so reaping can complete.

    Returns
    -------
    bool
        Whether the process had gone within *timeout* seconds. ``False`` for a
        handle that reported no PID, since nothing then can be confirmed.
    """
    if pid is None:
        return False
    return await _poll_until(lambda: not process_is_running(pid), timeout)


def _observe_start(child: ReadyChild) -> HandleObservation:
    """Start a blocking child and report what the handle exposed while it ran."""
    marker = child.marker

    async def orchestrate() -> HandleObservation:
        handle = await _start_owned(child.command)
        await _wait_for_ready(marker)
        observation = HandleObservation(
            handle_pid=handle.pid,
            exec_id=handle.exec_id,
            child_was_live=handle.pid is not None,
            result=handle.result,
        )
        await handle.kill()
        return dc.replace(observation, child_reaped=await _reaped(handle.pid))

    return asyncio.run(orchestrate())


def _observe_output_wait(command: SafeCmd) -> HandleObservation:
    """Await a child that writes, then ends with its own status."""

    async def orchestrate() -> HandleObservation:
        handle = await _start_owned(command)
        result = await handle.wait()
        return HandleObservation(
            result=result,
            handle_pid=handle.pid,
            child_reaped=await _reaped(handle.pid),
        )

    return asyncio.run(orchestrate())


def _observe_failed_start(command: SafeCmd) -> HandleObservation:
    """Start a program that is not on disk and carry out the failure."""

    async def orchestrate() -> HandleObservation:
        try:
            await _start_owned(command)
        except (FileNotFoundError, OSError) as failure:
            # Carried out rather than handled: which failure reaches the caller
            # is the scenario's subject, so it must not be absorbed here. The
            # set is the spawn failures a missing executable can produce.
            return HandleObservation(error=failure)
        return HandleObservation()

    return asyncio.run(orchestrate())


def _observe_early_exit_signal(
    command: SafeCmd,
    signal_number: int,
) -> HandleObservation:
    """Await a child that exited early, then attempt to signal it.

    The child is waited on first, so the handle has settled before the signal
    is attempted. That ordering is the realistic one: a caller coordinating a
    child that failed to come up learns it exited from the handle's own
    outcome, and only then finds that a signal has nowhere to go.

    Returns
    -------
    HandleObservation
        The settled outcome, and the refusal the signal attempt raised.
    """

    async def orchestrate() -> HandleObservation:
        handle = await _start_owned(command)
        result = await handle.wait()
        try:
            await handle.signal(signal_number)
        except ProcessLookupError as refusal:
            # Carried out rather than handled: that the refusal reached the
            # caller, and what it named, is the scenario's subject.
            return HandleObservation(
                result=result,
                error=refusal,
                handle_pid=handle.pid,
                child_reaped=await _reaped(handle.pid),
            )
        return HandleObservation(result=result, handle_pid=handle.pid)

    return asyncio.run(orchestrate())


def _observe_wait_deadline(command: SafeCmd) -> HandleObservation:
    """Wait on a blocking child under an already-elapsed deadline."""

    async def orchestrate() -> HandleObservation:
        handle = await _start_owned(command)
        try:
            await handle.wait(timeout=0)
        except TimeoutExpired as expiry:
            # Carried out rather than handled: the deadline surfacing as the
            # public timeout is the scenario's subject.
            return HandleObservation(
                error=expiry,
                handle_pid=handle.pid,
                child_reaped=await _reaped(handle.pid),
            )
        return HandleObservation(handle_pid=handle.pid)

    return asyncio.run(orchestrate())


def _observe_concurrent_waits(command: SafeCmd) -> HandleObservation:
    """Issue several waits on one handle at once and report what each saw."""
    waiters = 5

    async def orchestrate() -> HandleObservation:
        handle = await _start_owned(command)
        results = await asyncio.gather(*(handle.wait() for _ in range(waiters)))
        return HandleObservation(
            result=results[0],
            results=tuple(results),
            handle_pid=handle.pid,
            child_reaped=await _reaped(handle.pid),
        )

    return asyncio.run(orchestrate())
