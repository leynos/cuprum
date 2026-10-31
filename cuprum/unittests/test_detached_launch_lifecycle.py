"""What a detached launch does around the moment ownership changes hands.

The receipt is the only thing the caller keeps, so every question here is about
the boundary it marks. Before it exists cuprum still owns the worker and must
end one it cannot hand over; after it exists the caller owns the worker and
cuprum must not touch it. The cases below take each side of that line: a
cancellation that lands before the spawn, one that lands in the yield after the
transfer, a failure while building the receipt, and the ordinary launch that
succeeds.

The remaining cases are about what the caller inherits: a receipt is returned
even when the worker exits at once, because it reports a successful ``exec`` and
nothing more; dropping cuprum's process object leaves the worker running without
a ``ResourceWarning``; and the abandonment path itself is exercised against a
real child. Two cases reach past the receipt to the spawn: one interrupts
``Popen``'s constructor after the fork, the other cancels a real launch at the
transfer boundary, so the cancellation route is proved on a worker too.
"""

from __future__ import annotations

import asyncio
import gc
import os
import pathlib
import signal
import sys
import typing as typ
import warnings

import pytest

from cuprum import ScopeConfig, _detached_launch, _detached_spawn, scoped
from cuprum._detached_launch import _abandon_detached
from cuprum._detached_spawn import _DetachedPopen, _spawn_detached
from cuprum.context import bind_executable
from cuprum.sh import ExecutionContext
from tests.helpers.catalogue import build_python_catalogue_env
from tests.helpers.detached import (
    FAKE_PID,
    FakeProcess,
    record_abandonment,
)
from tests.helpers.process_state import process_state

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.program import Program
    from cuprum.sh import DetachedLaunch, SafeCmd

#: Every case here either spawns a worker or reaches the platform guard that
#: refuses a host without POSIX sessions, so none of them can run elsewhere.
pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="a detached launch requires POSIX session isolation",
)

#: Long enough that the worker cannot plausibly exit on its own, so a case that
#: finds it alive is not racing its natural exit.
_SLEEP_SOURCE = "import time; time.sleep(300)"

_FD_DIRECTORY = pathlib.Path("/proc/self/fd")


def _interpreter() -> tuple[cabc.Callable[..., SafeCmd], Program]:
    """Return a builder for the running interpreter and its allowlisted program."""
    env = build_python_catalogue_env()
    return env.builder, env.program


def _launch(*argv: str) -> DetachedLaunch:
    """Launch a Python command under a scope that allows the interpreter."""
    builder, program = _interpreter()
    with scoped(ScopeConfig(allowlist=frozenset({program}))):
        return builder(*argv).launch_detached_sync()


def _spawn_sleeper() -> _DetachedPopen:
    """Spawn a real sleeping worker and return cuprum's handle to it."""
    return _spawn_detached(
        (sys.executable, "-c", _SLEEP_SOURCE),
        env=None,
        cwd=None,
        start_new_session=True,
    )


def _is_running(pid: int) -> bool:
    """Report whether *pid* names a live, unreaped worker."""
    state = process_state(pid)
    if state.available:
        return not state.exited
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _open_descriptor_count() -> int:
    """Count this process's open descriptors, skipping hosts without procfs."""
    if not _FD_DIRECTORY.is_dir():
        pytest.skip("counting open descriptors requires procfs")
    return sum(1 for _ in _FD_DIRECTORY.iterdir())


class _InterruptedPopen(_DetachedPopen):
    """A process object interrupted after its child exists, as a signal handler does.

    ``Popen`` has forked and closed its descriptors, then propagates without
    terminating the child or handing back an object.
    """

    created: typ.ClassVar[list[int]] = []

    def __init__(self, argv: cabc.Sequence[str], **kwargs: object) -> None:
        """Spawn a real child, record its PID, and fail like an interrupted wait."""
        # ``kwargs`` are the spawn's own keywords, forwarded unchanged so this
        # double keeps mirroring whatever ``_spawn_detached`` passes.
        spawn_keywords = typ.cast("dict[str, typ.Any]", kwargs)
        super().__init__(argv, **spawn_keywords)
        type(self).created.append(self.pid)
        message = "interrupted while waiting for the exec report"
        raise KeyboardInterrupt(message)


def test_a_task_cancelled_before_it_runs_creates_no_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation before the coroutine starts leaves nothing to abandon."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        _detached_launch,
        "_spawn_detached",
        lambda argv, **_: calls.append(tuple(argv)),
    )
    builder, _ = _interpreter()
    command = builder("-c", "pass")

    async def run() -> None:
        """Cancel the launch before its coroutine ever runs."""
        task = asyncio.create_task(command.launch_detached())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert not calls, "a coroutine that never ran must not spawn a child"


def test_a_cancellation_requested_during_the_launch_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation delivered after the transfer stops the worker it orphaned.

    The spawn requests cancellation of the task running the launch, which can
    only be delivered at the yield after the transfer. The caller therefore
    never receives the receipt, and cuprum still owns the worker, so it must end
    it rather than leave a child nobody holds.
    """
    abandoned: list[int] = []
    spawned: list[int] = []

    def cancelling_spawn(argv: cabc.Sequence[str], **_: object) -> FakeProcess:
        """Request cancellation from inside the launch and report a PID."""
        spawned.append(FAKE_PID)
        task = asyncio.current_task()
        assert task is not None, "the launch must run inside a task"
        task.cancel()
        return FakeProcess()

    monkeypatch.setattr(_detached_launch, "_spawn_detached", cancelling_spawn)
    abandoned = record_abandonment(monkeypatch)
    builder, _ = _interpreter()
    command = builder("-c", "pass")

    async def run() -> None:
        """Await a launch that is cancelled from inside the spawn."""
        with pytest.raises(asyncio.CancelledError):
            await command.launch_detached()

    asyncio.run(run())

    assert spawned == [FAKE_PID], "the launch must reach the spawn exactly once"
    assert abandoned == [FAKE_PID], "the orphaned worker must be stopped"


def test_a_cancellation_at_the_transfer_boundary_reclaims_a_real_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real cancellation route stops and reaps the child it orphaned.

    The spawn double in the previous case proves the decision; this case proves
    the consequence, driving a real worker through the same boundary so the
    process group is actually signalled and the child actually reaped.
    """
    reclaimed: list[int] = []
    real_abandon = _detached_launch._abandon_detached

    def recording_abandon(pid: int) -> None:
        """Record the PID, then run the real kill-and-reap."""
        reclaimed.append(pid)
        real_abandon(pid)

    monkeypatch.setattr(_detached_launch, "_abandon_detached", recording_abandon)
    builder, program = _interpreter()
    command = builder("-c", _SLEEP_SOURCE)

    async def run() -> None:
        """Cancel the launch once it has spawned but before it returns."""
        with scoped(ScopeConfig(allowlist=frozenset({program}))):
            task = asyncio.create_task(command.launch_detached())
            asyncio.get_running_loop().call_soon(task.cancel)
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())

    [pid] = reclaimed
    assert not _is_running(pid), "the cancelled launch must stop its worker"
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_an_interrupted_spawn_reclaims_the_child_it_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constructor that fails after the fork must not leave a child running."""
    _InterruptedPopen.created.clear()
    monkeypatch.setattr(_detached_spawn, "_DetachedPopen", _InterruptedPopen)

    with pytest.raises(KeyboardInterrupt):
        _spawn_sleeper()

    [pid] = _InterruptedPopen.created
    assert not _is_running(pid), "the interrupted spawn must stop its child"
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_a_failure_while_building_the_receipt_stops_the_spawned_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt that cannot be built must not leak the child behind it."""

    def refusing_receipt(**_: object) -> object:
        """Fail the way an unreceiptable launch would."""
        raise RuntimeError

    monkeypatch.setattr(
        _detached_launch,
        "_spawn_detached",
        lambda argv, **_: FakeProcess(),
    )
    monkeypatch.setattr(_detached_launch, "DetachedLaunch", refusing_receipt)
    abandoned = record_abandonment(monkeypatch)
    builder, _ = _interpreter()

    with pytest.raises(RuntimeError):
        builder("-c", "pass").launch_detached_sync()

    assert abandoned == [FAKE_PID], "a spawn that yielded no receipt must be ended"


def test_a_missing_executable_fails_the_launch_instead_of_returning_a_receipt(
    tmp_path: pathlib.Path,
) -> None:
    """An executable that cannot be started surfaces as an error, not a receipt."""
    builder, program = _interpreter()
    absent = tmp_path / "absent-tool"

    with (
        scoped(ScopeConfig(allowlist=frozenset({program}))),
        bind_executable(program, str(absent)),
        pytest.raises(FileNotFoundError),
    ):
        builder("-c", "pass").launch_detached_sync()


def test_a_receipt_is_returned_for_a_worker_that_exits_at_once() -> None:
    """A receipt reports a successful ``exec``, not that the worker is alive."""
    receipt = _launch("-c", "pass")

    pid, status = os.waitpid(receipt.pid, 0)

    assert pid == receipt.pid
    assert os.waitstatus_to_exitcode(status) == 0


def test_dropping_the_process_object_leaves_the_worker_running() -> None:
    """Releasing cuprum's handle neither reclaims the child nor warns about it."""
    process = _spawn_sleeper()
    pid = process.pid
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            del process
            gc.collect()
        resource_warnings = [
            warning for warning in caught if warning.category is ResourceWarning
        ]
        assert not resource_warnings, (
            "dropping a detached process object must not report the worker"
        )
        assert _is_running(pid), "the worker must outlive cuprum's handle"
    finally:
        _abandon_detached(pid)
    assert not _is_running(pid), "the case must leave no worker behind"


def test_the_launch_leaves_no_descriptor_behind() -> None:
    """The spawned child's descriptors are the kernel's, not the launcher's."""
    before = _open_descriptor_count()

    receipt = _launch("-c", _SLEEP_SOURCE)
    try:
        after = _open_descriptor_count()
    finally:
        _abandon_detached(receipt.pid)

    assert after == before, "a detached launch must not keep a probe descriptor"


def test_abandoning_a_worker_kills_its_group_and_reaps_it() -> None:
    """The abandonment path leaves neither a live worker nor a zombie."""
    pid = _spawn_sleeper().pid

    _abandon_detached(pid)

    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def test_the_launch_schedules_no_background_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is left running on the caller's loop once the receipt is back."""
    monkeypatch.setattr(
        _detached_launch,
        "_spawn_detached",
        lambda argv, **_: FakeProcess(),
    )
    builder, _ = _interpreter()
    command = builder("-c", "pass")

    async def run() -> int:
        """Return how many tasks remain once the launch has completed."""
        await command.launch_detached()
        return len(asyncio.all_tasks())

    assert asyncio.run(run()) == 1, "only the launching task may remain"


def test_supervision_settings_cannot_change_the_ownership_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached launch ignores the settings a managed run is supervised by.

    ``process_group``, timeouts, and capture sinks describe how a run is
    supervised. A detached launch supervises nothing, so supplying them must not
    change what it does: the child still gets a session of its own.
    """
    session_flags: list[bool] = []

    def recording_spawn(argv: cabc.Sequence[str], **kwargs: object) -> FakeProcess:
        """Record the requested session isolation and report a PID."""
        session_flags.append(typ.cast("bool", kwargs["start_new_session"]))
        return FakeProcess()

    monkeypatch.setattr(_detached_launch, "_spawn_detached", recording_spawn)
    builder, program = _interpreter()

    with scoped(ScopeConfig(allowlist=frozenset({program}))):
        builder("-c", "pass").launch_detached_sync(
            context=ExecutionContext(process_group="inherit", timeout=0.001)
        )

    assert session_flags == [True], (
        "detachment must not inherit a caller's group policy"
    )
