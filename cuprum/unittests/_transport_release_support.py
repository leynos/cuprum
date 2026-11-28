"""Real children, real descriptors, and both spawn routes for release cases.

A case that asks whether a run released its parent-side read descriptors cannot
be judged against a stub: the guarantee is about what the kernel holds, and a
fake transport holds nothing. What these cases share is therefore the machinery
for owning a real child, not a substitute for one, plus the one observation
that distinguishes "the reader settled" from "the descriptor closed".

The child shapes here all leave a descendant holding the inherited write ends,
and they differ only in when the parent stops: the announcing holder never
exits, which lets a case stop a run before its exit wait settles, while
``tests.helpers.timeouts.descendant_then_exit_argv`` gives a child that exits
and leaves the descendant behind, which is the post-exit route. In both, no
end-of-file ever arrives, so only the run's own release can close the parent's
descriptor.

The backend axis is not cosmetic. The ``wait4`` owner reaps without closing
anything and the fallback's asyncio transports close only at end-of-file, so
each backend fails a closure assertion differently; a case that exercised only
the host's preferred route would be evidence about that route alone.
"""

from __future__ import annotations

import dataclasses as dc
import importlib
import os
import time
import typing as typ

import pytest

from cuprum import _wait4_spawn
from cuprum._pipeline_stream_fds import _extract_stream_fd
from cuprum.unittests._post_exit_teardown_support import (
    recorded_descendant,
    recorded_descendant_or_fail,
)
from tests.helpers.process_exit import ExitObservation, observe_exit
from tests.helpers.process_state import process_state

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc
    import pathlib
    import subprocess

    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._wait4_spawn import DirectProcessConfig

__all__ = [
    "_ANNOUNCING_HOLDER_SCRIPT",
    "_DESCENDANT_LIFETIME_S",
    "_WATCHDOG_S",
    "BackendTraversal",
    "SpawnRecord",
    "assert_descendant_holds_the_pipes",
    "assert_direct_child_reaped",
    "assert_output_transports_released",
    "assert_released_before_the_descendant_ends",
    "await_recorded_pid",
    "record_spawn",
    "spawn_backend",
]

#: Long enough that a run which waited for the child or its descendant could
#: not finish inside a case's own watchdog.
_DESCENDANT_LIFETIME_S = 30.0

#: An outer bound per case, so a regression fails rather than hangs the suite.
_WATCHDOG_S = 20.0

#: Spawns a descendant that inherits this child's output pipes and asks for its
#: own session, so it survives the direct child's termination and no group
#: signal reaches it. The pid is recorded for the case's cleanup and liveness
#: assertions. It then announces itself on stdout, which is what lets a case
#: deliver a line while the descendant is *already* holding the write ends: the
#: announcement is deliberately written after the descendant has recorded
#: itself, so an ``on_line`` failure or a caller's close cannot race the fork.
#: A line written earlier would leave the pipe free to reach end-of-file when
#: the child is terminated, and the case would pass without any release.
_ANNOUNCING_HOLDER_SCRIPT = """\
import pathlib
import subprocess
import sys
import time

lifetime = sys.argv[2]
descendant = subprocess.Popen(
    [sys.executable, "-c", f"import time; time.sleep({lifetime})"],
    stdin=subprocess.DEVNULL,
    stdout=sys.stdout,
    stderr=sys.stderr,
    start_new_session=True,
)
pathlib.Path(sys.argv[1]).write_text(str(descendant.pid), encoding="ascii")
sys.stdout.write("holding\\n")
sys.stdout.flush()
time.sleep(float(lifetime))
"""


@dc.dataclass(frozen=True, slots=True)
class _DescriptorIdentity:
    """One parent-side descriptor and the file it named when recorded.

    The identity is what makes a later reading trustworthy. ``fstat`` alone
    cannot distinguish "this descriptor is still open" from "this descriptor
    number was closed and reused", and a reused number is exactly what a suite
    that opens and closes many pipes produces.
    """

    fd: int
    st_dev: int
    st_ino: int


@dc.dataclass(slots=True)
class SpawnRecord:
    """What one wrapped spawn observed, for the case to assert on afterwards.

    The process and its descriptors are recorded together because the claim is
    about *that* child's pipes: reading descriptors from a different spawn, or
    from a process the run has already replaced, would answer a different
    question than the case asks.
    """

    process: asyncio.subprocess.Process | None = None
    outputs: tuple[_DescriptorIdentity, ...] = ()


class _SpawnFn(typ.Protocol):
    """The spawn entry a route resolves, as the recorder calls it."""

    def __call__(
        self, execution: _SubprocessExecution
    ) -> cabc.Awaitable[asyncio.subprocess.Process]: ...


def _identify_output_descriptors(
    process: asyncio.subprocess.Process,
) -> tuple[_DescriptorIdentity, ...]:
    """Record both output descriptors with the identity ``fstat`` reports."""
    identities: list[_DescriptorIdentity] = []
    for stream in (process.stdout, process.stderr):
        fd = _extract_stream_fd(stream)
        if fd is None:
            continue
        status = os.fstat(fd)
        identities.append(_DescriptorIdentity(fd, status.st_dev, status.st_ino))
    return tuple(identities)


def record_spawn(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    *,
    attribute: str = "_spawn_subprocess",
) -> SpawnRecord:
    """Wrap the spawn a route resolves, recording the child and its pipes.

    The module is named rather than passed so each case states the seam its
    route actually reads. Patching the definition instead would leave a route
    that imported the name at module load still calling the real spawn, and the
    case would report no child at all rather than a wrong route.

    Returns
    -------
    SpawnRecord
        The record the wrapper fills in when the route spawns.
    """
    module = importlib.import_module(module_name)
    real_spawn = typ.cast("_SpawnFn", getattr(module, attribute))
    record = SpawnRecord()

    async def spawn(execution: _SubprocessExecution) -> asyncio.subprocess.Process:
        """Spawn the real child, then record the descriptors it opened."""
        process = await real_spawn(execution)
        record.process = process
        record.outputs = _identify_output_descriptors(process)
        return process

    monkeypatch.setattr(module, attribute, spawn)
    return record


def _descriptor_is_open(identity: _DescriptorIdentity) -> bool:
    """Report whether *identity*'s number still names the same file.

    A number that now names a different file has been closed and reused, which
    is a released descriptor as much as one that raises ``EBADF`` is.

    Returns
    -------
    bool
        ``True`` while the descriptor still names the file it named before.
    """
    try:
        status = os.fstat(identity.fd)
    except OSError:
        return False
    return (status.st_dev, status.st_ino) == (identity.st_dev, identity.st_ino)


def assert_output_transports_released(record: SpawnRecord, *, context: str) -> None:
    """Assert the route closed both of the child's parent-side descriptors.

    Parameters
    ----------
    record : SpawnRecord
        What the wrapped spawn observed for the route under test.
    context : str
        The route's name, used in the failure. The same assertion runs for
        several routes, so a bare "descriptors leaked" would not say which.
    """
    assert record.process is not None, (
        f"{context} never spawned a child through the wrapped entry, so the "
        "case cannot have reached the run it claims to test"
    )
    assert len(record.outputs) == 2, (
        f"{context} must own a parent-side descriptor for both output streams "
        f"for this case to prove anything, got {record.outputs!r}"
    )
    still_open = [
        identity for identity in record.outputs if _descriptor_is_open(identity)
    ]
    assert not still_open, (
        f"{context} left descriptors {still_open!r} open while the descendant "
        "still held the write ends, so the parent-side pipes leaked"
    )


def assert_direct_child_reaped(
    process: asyncio.subprocess.Process,
    *,
    context: str,
) -> None:
    """Assert the direct child's exit status was collected, not merely that it died.

    Two observations, because neither is sufficient on its own. ``returncode``
    is published by whichever owner collected the status — the ``wait4`` owner
    only once its ``wait4`` call returns, and asyncio's transport only once the
    child watcher has reaped the child — so a non-``None`` value *is* the
    reaping, on either backend and either platform. The process table then
    rules out what a bare exit probe cannot see: ``observe_exit`` counts an
    unreaped zombie as exited, so a run that terminated the child and abandoned
    the reap would pass an exit check while leaving the zombie behind.
    """
    assert process.returncode is not None, (
        f"{context} must reap its direct child before the failure reaches the "
        f"caller, but pid {process.pid} publishes no exit status"
    )
    state = process_state(process.pid)
    if not state.available:
        # No readable process table on this host, so the collected status is
        # the strongest evidence available; it is still the reaping itself.
        return
    assert state.state != "Z", (
        f"{context} must reap its direct child, but pid {process.pid} is still "
        "an unreaped zombie"
    )


def await_recorded_pid(
    pid_file: pathlib.Path,
    *,
    timeout_s: float = 10.0,
) -> bool:
    """Block until the child has published a readable descendant pid, or time out.

    The blocking read exists for the one caller that must make the fork happen
    *before* it acts: a synchronous ``start`` hook runs inline on the event
    loop, and the child writing the file is a separate process, so it makes
    progress without the loop. Sleeping the loop there would instead let the
    run's own teardown start first, which is the race this avoids.

    The condition is a *parsed* pid, not the file's existence. A writer creates
    and truncates the path before it writes, so an existence check can return
    while the file is still empty and hand the caller a run that has not
    reached the fork it depends on.

    Returns
    -------
    bool
        ``True`` once the file holds a pid, ``False`` when the wait expired.
    """
    deadline = time.monotonic() + timeout_s
    while recorded_descendant(pid_file) is None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def assert_descendant_holds_the_pipes(descendant_pid: int, *, context: str) -> None:
    """Assert the descendant is still alive when the descriptors are read.

    Read this *before* the closure assertion. Descriptors having been released
    proves nothing if the pipes could already have reached end-of-file on their
    own, and the descendant being alive is what rules that out.
    """
    observed = observe_exit(descendant_pid)
    if observed is ExitObservation.UNOBSERVABLE:
        pytest.skip("this host cannot report whether a process has exited")
    assert observed is ExitObservation.RUNNING, (
        f"{context} must leave the descendant holding the write ends when the "
        f"descriptors are read, but pid {descendant_pid} was observed "
        f"{observed.name}"
    )


def assert_released_before_the_descendant_ends(
    record: SpawnRecord,
    pid_file: pathlib.Path,
    *,
    context: str,
) -> int:
    """Assert the run ended first, and closed both descriptors despite it.

    The order of the checks is the claim. A descriptor that is closed proves
    nothing on its own, because the pipe may have reached end-of-file when the
    child died; the descendant still holding the write ends is what rules that
    out, and the child having been reaped is what says the run ended at all.

    Parameters
    ----------
    record : SpawnRecord
        What the wrapped spawn observed for the route under test.
    pid_file : pathlib.Path
        Where the child recorded its descendant's pid.
    context : str
        The route's name, used in each failure.

    Returns
    -------
    int
        The descendant's pid, so the caller can reap it in ``finally``.
    """
    descendant_pid = recorded_descendant_or_fail(pid_file)
    assert record.process is not None, (
        f"{context} never spawned a child through the wrapped entry, so the "
        "case cannot have reached the run it claims to test"
    )
    assert_direct_child_reaped(record.process, context=context)
    assert_descendant_holds_the_pipes(descendant_pid, context=context)
    assert_output_transports_released(record, context=context)
    return descendant_pid


@dc.dataclass(frozen=True, slots=True)
class BackendTraversal:
    """The spawn route a case selected, and the routes actually reached."""

    requested: str
    reached: list[str]


def _recording_entry(
    reached: list[str], label: str, selected: str
) -> cabc.Callable[[DirectProcessConfig], subprocess.Popen[bytes]]:
    """Wrap ``_spawn_popen`` to record the ``wait4`` route and trap the other.

    On the ``wait4`` arm the record is a side effect: the wrapped entry is
    still called and its child still starts. On the fallback arm the wrapped
    entry is a trap, because reaching it means the availability patch did not
    take effect and the case ran the route it names as not selected.

    Returns
    -------
    collections.abc.Callable[[DirectProcessConfig], subprocess.Popen[bytes]]
        The entry point, recording on the way through, or refusing.
    """
    original = _wait4_spawn._spawn_popen

    def record(config: DirectProcessConfig) -> subprocess.Popen[bytes]:
        """Note the ``wait4`` route, then spawn or refuse."""
        reached.append(label)
        if selected != label:
            pytest.fail(
                "the wait4 spawn entry was reached, but this case selected "
                f"{selected!r}; the availability patch did not take effect"
            )
        return original(config)

    return record


def _recording_transport(
    reached: list[str], label: str, selected: str
) -> cabc.Callable[..., cabc.Coroutine[object, object, object]]:
    """Wrap ``create_subprocess_exec`` to record the fallback and trap the other.

    The wrapper forwards through a cast rather than mirroring the transport's
    own parameters; the arguments are still checked against the real callee's
    signature at the call, so a drifted wrapper fails loudly rather than
    passing silently.

    Returns
    -------
    collections.abc.Callable[..., collections.abc.Coroutine[object, object, object]]
        The transport, recording on the way through, or refusing.
    """
    original = typ.cast(
        "cabc.Callable[..., cabc.Coroutine[object, object, object]]",
        _wait4_spawn.asyncio.create_subprocess_exec,
    )

    async def record(*args: object, **kwargs: object) -> object:
        """Note the asyncio route, then run or refuse the real transport."""
        reached.append(label)
        if selected != label:
            pytest.fail(
                "the asyncio transport was reached, but this case selected "
                f"{selected!r}; the wait4 entry patch did not take effect"
            )
        return await original(*args, **kwargs)

    return record


@pytest.fixture(params=["wait4", "asyncio"], ids=["wait4", "asyncio"])
def spawn_backend(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> cabc.Iterator[BackendTraversal]:
    """Select the spawn backend a case exercises, and record that it was taken.

    Both arms wrap *both* routes: the selected route records and calls through,
    and the other refuses. A label alone changes a case's name; the trap is
    what makes the axis real, so a case that stopped selecting its route fails
    naming the route that ran instead of quietly passing as evidence about the
    host's preferred one.

    The ``wait4`` arm skips where ``os.wait4`` is unavailable, rather than
    running the fallback under a ``wait4`` label.

    Yields
    ------
    BackendTraversal
        The route this case selected and the routes whose entry was reached.
    """
    reached: list[str] = []
    if request.param == "wait4":
        if not _wait4_spawn.wait4_resource_measurement_available():
            pytest.skip("os.wait4 and waitstatus_to_exitcode are unavailable here")
    else:
        # ``spawn_direct_process`` reads this at call time, so patching it
        # selects the fallback for this case alone. Patching the dispatch
        # itself would replace the branch under test.
        monkeypatch.setattr(
            _wait4_spawn, "wait4_resource_measurement_available", lambda: False
        )
    monkeypatch.setattr(
        _wait4_spawn,
        "_spawn_popen",
        _recording_entry(reached, "wait4", request.param),
    )
    monkeypatch.setattr(
        _wait4_spawn.asyncio,
        "create_subprocess_exec",
        _recording_transport(reached, "asyncio", request.param),
    )
    traversal = BackendTraversal(request.param, reached)
    yield traversal
    assert traversal.reached == [traversal.requested], (
        "this case selected the "
        f"{traversal.requested!r} route, but the routes actually reached were "
        f"{traversal.reached!r}; the arm did not take effect, so the case is "
        "evidence about whichever route the host preferred rather than about "
        "the one it claims"
    )
