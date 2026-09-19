"""The failure paths ``cuprum._wait4_process`` owns but a live run never takes.

A command that runs to completion exercises one branch of this module: spawn,
attach pipes, reap once. Two other branches exist only for things going wrong,
and both protect the same promise — that ``_Wait4Process._reap`` is the only
reaper. Nothing a caller can observe through ``CommandResult`` separates "the
child was cleaned up" from "the child leaked", so neither branch is covered by
running a command and reading its result. These tests drive them directly.

Each test spawns a real child rather than a stub, because the guarantees under
test are about process lifetime: the cleanup branch must leave no zombie, and
the mismatch branch must not adopt another child's exit status. A fake process
can demonstrate neither.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import types

import pytest

from cuprum import _wait4_process

_wait4_only = pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="asserts direct-child wait4 process ownership",
)

# A child that outlives the test until something reaps or signals it, so a
# missing cleanup shows up as a live process rather than a fast exit. The odd
# duration is deliberate: it is long enough to outlive any run here and
# distinctive enough that a process a failed run did strand can be told apart
# from every other sleeper on a shared machine.
_SLEEP_FOREVER = ("sleep", "997")


class _FakeUsage(types.SimpleNamespace):
    """The resource record shape ``wait4`` returns, with placeholder figures.

    It subclasses ``SimpleNamespace`` rather than merely resembling it so the
    test's stub matches the type its call site declares, and the mismatch this
    test provokes is the pid — not the usage shape.
    """

    def __init__(self) -> None:
        """Populate the three fields ``resource_usage_from_wait4`` reads."""
        super().__init__(ru_maxrss=4, ru_utime=0.5, ru_stime=0.25)


def _sleep_config() -> _wait4_process.DirectProcessConfig:
    """Build spawn inputs for a child that stays alive until signalled.

    Returns
    -------
    DirectProcessConfig
        Inputs that request no pipes, keeping the spawn free of stream setup.
    """
    return _wait4_process.DirectProcessConfig(
        argv=_SLEEP_FOREVER,
        stdin=None,
        stdout=None,
        stderr=None,
        env=None,
        cwd=None,
    )


def _still_owns_child(pid: int) -> bool:
    """Return whether ``pid`` is still this process's unreaped child.

    Any successful ``waitpid`` means the child is still ours, whether it is
    still running (``(0, 0)`` under ``WNOHANG``) or sitting as a zombie
    (``(pid, status)``). Only the failure to find it at all proves the child
    was collected, so the exception — not the returned status — decides this.

    Returns
    -------
    bool
        True when this process still owns an unreaped child at ``pid``.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    return True


def _abandon(pid: int) -> None:
    """Best-effort cleanup so a failing assertion cannot leak a live child."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    # Someone else may have reaped it first; there is nothing left to collect.
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)


@_wait4_only
def test_pipe_connection_failure_kills_and_reaps_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pipe attachment must not strand the child it already spawned.

    The child exists before its pipes do, so a failure while attaching them
    would otherwise leave a live process with no owner and no result to carry
    its usage. The cleanup has to do both halves: a kill alone still leaves a
    zombie for this process to reap later.
    """
    spawned: list[int] = []

    async def failing_connect(  # ruff: ignore[unused-async] - stands in for an awaited coroutine method.
        self: _wait4_process._Wait4Process,
    ) -> None:
        """Record the live pid, then fail as a broken pipe attachment would."""
        spawned.append(self.pid)
        msg = "simulated pipe attachment failure"
        raise OSError(msg)

    monkeypatch.setattr(_wait4_process._Wait4Process, "connect_pipes", failing_connect)

    async def spawn_and_fail() -> None:
        """Require the attachment failure to reach the caller."""
        with pytest.raises(OSError, match="simulated pipe attachment failure"):
            await _wait4_process.spawn_wait4_process(_sleep_config())

    asyncio.run(spawn_and_fail())

    assert spawned, "the spawn must have produced a child before the failure"
    pid = spawned[0]
    leaked = _still_owns_child(pid)
    if leaked:
        _abandon(pid)
    assert not leaked, (
        f"the cleanup must kill and reap pid {pid}, but it is still an "
        "unreaped child of this process"
    )


@_wait4_only
def test_reap_rejects_a_child_this_owner_did_not_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wait4`` naming another pid must fail instead of adopting its status.

    ``os.wait4`` returns whichever child it reaped. Publishing that status and
    its usage would attribute a stranger's exit code and resource figures to
    this command, silently and undetectably, so the mismatch has to fail and
    leave both published values unset.
    """

    def fake_wait4(pid: int, options: int) -> tuple[int, int, types.SimpleNamespace]:
        """Report a reaped pid that is not the one this owner spawned.

        Returns
        -------
        tuple[int, int, types.SimpleNamespace]
            A pid one greater than expected, with a placeholder status.
        """
        del options
        return (pid + 1, 0, _FakeUsage())

    spawned: list[int] = []

    async def spawn_and_reap() -> tuple[
        _wait4_process._Wait4Process, BaseException | None
    ]:
        """Spawn a real child, then reap it through a mismatching ``wait4``."""
        process = await _wait4_process.spawn_wait4_process(_sleep_config())
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        spawned.append(process.pid)
        monkeypatch.setattr(_wait4_process.os, "wait4", fake_wait4)
        failure: BaseException | None = None
        try:
            await process.wait()
        except BaseException as exc:  # ruff: ignore[blind-except] - the test asserts the exact type below.
            failure = exc
        return process, failure

    try:
        process, failure = asyncio.run(spawn_and_reap())
    finally:
        # Signalling uses the raw pid rather than ``process.kill()``: that
        # method is part of the code under test, and a defect it exists to
        # prevent must not be able to turn this cleanup into a no-op. The
        # blocking collect runs here, off the event loop.
        for pid in spawned:
            _abandon(pid)
    assert not _still_owns_child(process.pid), (
        "the mismatch branch must not have reaped the real child either"
    )

    assert isinstance(failure, _wait4_process._Wait4InvariantError), (
        f"a mismatched reap must raise the wait4 invariant error, got {failure!r}"
    )
    assert failure.expected_pid == process.pid, (
        "the error must name the pid this owner spawned"
    )
    assert failure.reaped_pid == process.pid + 1, (
        "the error must name the pid wait4 actually reported"
    )
    assert isinstance(failure, _wait4_process._ExecutionInvariantError), (
        "the error must stay inside the internal invariant hierarchy"
    )
    assert process.returncode is None, (
        "a rejected reap must not publish an exit code it did not establish"
    )
    assert process.resource_usage is None, (
        "a rejected reap must not publish another child's resource usage"
    )
