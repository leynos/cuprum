"""Child builders and the shared observation record for the handle scenarios.

Three companion modules split the handle's behavioural scaffolding by subject:
:mod:`tests.behaviour._running_command_control_support` drives the stop,
signal, and teardown scenarios, :mod:`tests.behaviour._running_command_steps`
holds the Given and When steps,
:mod:`tests.behaviour._running_command_support` drives start and wait, and this
one builds the children all of them drive. Every module carries a leading
underscore so pytest does not collect it as a test module.

The children come from :mod:`tests.helpers.timeouts`, so these scenarios drive
the same child protocol as the existing timeout tests rather than introducing
a second one: a child that writes identifiable output on both streams, records
a readiness marker, and then blocks for several minutes.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import os
import pathlib
import sys
import typing as typ

from cuprum import sh
from cuprum.catalogue import ProgramCatalogue
from cuprum.program import Program
from tests.helpers.catalogue import python_catalogue
from tests.helpers.process_state import parent_pid_from_stat, process_state
from tests.helpers.timeouts import (
    CHILD_STDOUT,
    child_argv,
    process_is_running,
    stubborn_child_argv,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.events import ExecId
    from cuprum.sh import CommandResult, SafeCmd

__all__ = [
    "DELAYED_WRITER_MARKER",
    "_EARLY_EXIT_CODE",
    "_OUTPUT_EXIT_CODE",
    "HandleObservation",
    "ReadyChild",
    "_absent_start_command",
    "_delayed_writer_command",
    "_exit_code_command",
    "_poll_until",
    "_python_builder",
    "_ready_child",
    "_reaped",
    "_sleep_command",
    "_wait_until_exited",
    "live_child_pids",
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
# Both templates take the status as a parameter for the same reason: a caller
# that names one status and receives another has no way to notice, and the
# statuses here are deliberately distinct from zero and from each other so a
# wrong one cannot pass by coincidence. Hard-coding one of them into the
# output-writing template is exactly how that guarantee is lost.
_OUTPUT_EXIT_SOURCE = (
    "import sys\n"
    f"sys.stdout.write({CHILD_STDOUT!r} + chr(10))\n"
    "sys.stdout.flush()\n"
    "sys.exit({code})\n"
)
_DELAYED_WRITER_SOURCE = (
    f"import time\ntime.sleep(0.2)\nprint({DELAYED_WRITER_MARKER!r}, flush=True)\n"
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
    unsettled_when_refused : bool
        Whether the handle had settled nothing at the moment its operation was
        refused. The scenario that signals a child it never waited on sets
        this, because that is the whole of its subject: a held handle publishes
        no status, so a refusal carrying the child's real one cannot have read
        it from the handle.
    handle_pid : int | None
        The PID the handle reported, as its ``start`` event announced it.
    exec_id : ExecId | None
        The execution identifier the handle carried, or ``None`` for a scenario
        that never got as far as reading one.
    child_was_live : bool
        Whether the child was still running when ``start`` returned.
    child_reaped : bool
        Whether the child had gone by the time the scenario finished. Checked
        rather than assumed, so a leaked child fails its scenario instead of
        being left behind.
    spawned_children : frozenset[int]
        The child PIDs that appeared during the scenario, derived by comparing
        the test process's children before and after it. A scenario whose
        spawn must not happen asserts this is empty; a handle that reports no
        PID for a start that failed gives its own fields no way to witness a
        leak, so this reads the system instead.
    """

    result: CommandResult | None = None
    results: tuple[CommandResult, ...] = ()
    error: BaseException | None = None
    unsettled_when_refused: bool = False
    handle_pid: int | None = None
    exec_id: ExecId | None = None
    child_was_live: bool = False
    child_reaped: bool = False
    spawned_children: frozenset[int] = frozenset()


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
    template = _OUTPUT_EXIT_SOURCE if writes else _EXIT_SOURCE
    return _script_command(tmp_path, "exits.py", template.format(code=code))


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


async def _wait_until_exited(pid: int, timeout: float = _READY_TIMEOUT) -> None:
    """Wait until the child *pid* has exited but is not necessarily reaped.

    A child that has exited and not been reaped remains visible in procfs as a
    zombie, which is the window this waits for. Waiting on that fact rather
    than on elapsed time is what makes the scenario deterministic: a handle
    whose child is still starting would refuse nothing, so a signal sent early
    would test the wrong thing.

    Raises
    ------
    TimeoutError
        If the child has not exited within *timeout* seconds.
    """
    if not await _poll_until(lambda: process_state(pid).exited, timeout):
        msg = f"child {pid} had not exited within {timeout}s"
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


def live_child_pids() -> frozenset[int]:
    """Return the PIDs this process currently has as direct children.

    A start that fails before a child exists reports no PID, so the handle's
    own fields cannot witness whether one leaked. This is the fact that can:
    the children of this test process, read from procfs, before and after the
    attempt. A run that spawned and abandoned a child differs between the two
    readings; a run that never spawned one does not. Each child's parent is
    read with :func:`tests.helpers.process_state.parent_pid_from_stat`, which
    is where the procfs ``stat`` parsing lives.

    Returns
    -------
    frozenset[int]
        The direct child PIDs, empty on a host without procfs — where the
        comparison is then vacuous and the behaviour tests are already
        Linux-scoped.
    """
    if sys.platform != "linux":
        return frozenset()
    own_pid = os.getpid()
    children: set[int] = set()
    try:
        entries = tuple(pathlib.Path("/proc").iterdir())
    except OSError:
        return frozenset()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            # The process exited between the listing and this read; it was
            # never reported, because it is no longer running to report.
            continue
        if parent_pid_from_stat(stat) == own_pid:
            children.add(pid)
    return frozenset(children)
