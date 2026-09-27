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
import enum
import os
import signal
import sys
import types
import typing as typ

import pytest

from cuprum import _wait4_process

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_wait4_only = pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="asserts direct-child wait4 process ownership",
)

# Enough to overrun the host's pipe buffer (65 536 bytes here) several times
# over, which is what turns "nobody reads this pipe" into a blocked child
# rather than a merely wasteful one. The margin is deliberate: a buffer of any
# size the kernel could plausibly report is still smaller than this, so the
# test cannot pass for the wrong reason on a machine with a larger buffer.
_OVERRUNS_ANY_PIPE_BUFFER = 4 * 1024 * 1024

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
        pipes=frozenset(),
    )


class _ChildOwnership(enum.Enum):
    """What a ``WNOHANG`` probe found at a pid this test spawned."""

    #: Still running: nothing has collected it and it has not exited.
    LIVE = enum.auto()
    #: Exited but unreaped. The probe itself reaped it, so the pid is now a
    #: free slot the kernel can hand to an unrelated process.
    REAPED = enum.auto()
    #: Not this process's child at all; someone else collected it first.
    COLLECTED = enum.auto()


def _probe_child(pid: int) -> _ChildOwnership:
    """Report what this process still owns at ``pid``.

    A successful ``waitpid`` under ``WNOHANG`` returns the pid once the child
    has exited, and reaps it as a side effect; it returns ``(0, 0)`` while the
    child is still running, having reaped nothing. Only the exception proves
    the child was collected by someone else. The two non-exceptional outcomes
    are therefore not the same answer, and the caller has to act on them
    differently: a live child is ours to kill, whereas a pid the probe reaped
    is already collected and — because that slot is now available — must not
    be signalled again.

    Returns
    -------
    _ChildOwnership
        ``COLLECTED``, ``REAPED``, or ``LIVE``, in that order of precedence.
    """
    try:
        waited_pid, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return _ChildOwnership.COLLECTED
    if waited_pid == pid:
        return _ChildOwnership.REAPED
    return _ChildOwnership.LIVE


def _abandon(pid: int) -> None:
    """Best-effort cleanup so a failing assertion cannot leak a live child."""
    if _probe_child(pid) is not _ChildOwnership.LIVE:
        # Nothing is running at this pid that this test owns. A pid the probe
        # reaped may already have been recycled onto an unrelated process, so
        # signalling it would kill a stranger rather than clean up after this
        # test.
        return
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
    # The probe reaps a zombie as it reports one, so whatever it finds is
    # cleaned up either way: `_abandon` kills a live child, and a child the
    # probe already reaped is by definition collected. Only `LIVE` is a leak.
    ownership = _probe_child(pid)
    _abandon(pid)
    assert ownership is not _ChildOwnership.LIVE, (
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
        _wait4_process._Wait4Process,
        _wait4_process._Wait4InvariantError,
        _ChildOwnership,
    ]:
        """Spawn a real child, then reap it through a mismatching ``wait4``."""
        process = await _wait4_process.spawn_wait4_process(_sleep_config())
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        spawned.append(process.pid)
        monkeypatch.setattr(_wait4_process.os, "wait4", fake_wait4)
        # Narrow enough to state the contract: the mismatch must raise this
        # error and nothing else. A bare `except BaseException` would also
        # absorb a `CancelledError` or an unrelated failure and let the
        # assertions below report it as the wrong type.
        with pytest.raises(_wait4_process._Wait4InvariantError) as caught:
            await process.wait()
        # Probed here, before the caller's cleanup reaps it: this is the only
        # point at which the test can still see what the mismatch branch left
        # behind. The child is an unexited `sleep`, so a branch that respected
        # the mismatch leaves it LIVE and unreaped.
        return process, caught.value, _probe_child(process.pid)

    try:
        process, failure, ownership = asyncio.run(spawn_and_reap())
    finally:
        # Signalling uses the raw pid rather than ``process.kill()``: that
        # method is part of the code under test, and a defect it exists to
        # prevent must not be able to turn this cleanup into a no-op. The
        # blocking collect runs here, off the event loop.
        for pid in spawned:
            _abandon(pid)
    assert ownership is _ChildOwnership.LIVE, (
        "the mismatch branch must not have reaped the real child either, but "
        f"the child was found {ownership!r}"
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


def test_unread_pipe_stays_devnull_on_the_fallback_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback must not turn an unread stream's ``DEVNULL`` into a pipe.

    A pipe target with nothing consuming it resolves to ``DEVNULL`` before the
    spawn, and that value has to reach ``create_subprocess_exec`` unchanged.
    Rewriting it to ``PIPE`` — because the stream appears in ``pipes`` — leaves
    the parent holding a pipe nobody reads, so a child that writes past the
    buffer blocks in ``write`` and the run hangs instead of discarding the
    output.

    Two assertions cover that, deliberately in this order. The first checks the
    value the backend was handed, which is where the two outcomes differ and
    which fails in milliseconds naming the wrong value. The second awaits the
    overrunning child, so the test also proves the run completes rather than
    merely that the right sentinel was passed; the overrun is sized past any
    plausible buffer so a machine with a large one cannot make it pass for the
    wrong reason.

    The value check runs *before* the await on purpose. Under the promotion bug
    the child is stuck in ``write`` for as long as the parent declines to read,
    so awaiting first would report the defect as a timeout — the wrong reason —
    and would leave the blocked child behind. The ``finally`` collects whatever
    is left instead.
    """
    monkeypatch.setattr(
        _wait4_process, "wait4_resource_measurement_available", lambda: False
    )
    spawn = _wait4_process.asyncio.create_subprocess_exec
    observed: dict[str, int | None] = {}

    async def _record(
        *argv: str,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: cabc.Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> asyncio.subprocess.Process:
        """Record the stdio the fallback was handed, then spawn for real.

        The keywords are spelled out rather than taken as ``**kwargs``: a
        ``**kwargs`` annotation has to describe every parameter the real
        ``create_subprocess_exec`` accepts, so a narrower one is rejected at the
        splat and a wider one would have to be ``Any``. Naming the five the
        fallback actually sends keeps the recording and the call in agreement.

        Returns
        -------
        asyncio.subprocess.Process
            The process the real spawn produced, so the test can await it.
        """
        observed.update({"stdin": stdin, "stdout": stdout, "stderr": stderr})
        return await spawn(
            *argv, stdin=stdin, stdout=stdout, stderr=stderr, env=env, cwd=cwd
        )

    monkeypatch.setattr(_wait4_process.asyncio, "create_subprocess_exec", _record)

    async def run_child() -> None:
        """Run a child that writes far more than a pipe buffer, unread."""
        config = _wait4_process.DirectProcessConfig(
            argv=(
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write('x' * {_OVERRUNS_ANY_PIPE_BUFFER})",
            ),
            stdin=None,
            # Computed exactly as the spawn layer computes it: a pipe target
            # that nothing consumes. ``None`` would be inherited, not
            # discarded, and the child would block on a full pipe for a
            # different reason than the one under test.
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=None,
            cwd=None,
            pipes=frozenset({"stdout", "stderr"}),
        )
        process = await _wait4_process.spawn_direct_process(config)
        try:
            # Checked here, on the value the backend was handed and before the
            # child is awaited: a no-op in the passing case, and in the failing
            # case the only assertion that can run at all, since the child is
            # blocked in ``write`` for as long as the parent declines to read.
            assert observed.get("stdout") == asyncio.subprocess.DEVNULL, (
                "an unread stream must reach the fallback as DEVNULL, not as "
                f"a pipe the parent never reads; got {observed.get('stdout')!r}"
            )
            returncode = await asyncio.wait_for(process.wait(), timeout=30)
        finally:
            # A child left mid-``write`` on an unread pipe outlives the run
            # that stranded it, so it is both signalled and collected here
            # rather than abandoned to the interpreter's exit. ``kill`` raises
            # once the child has already been collected, which is exactly the
            # passing case; the ``wait`` below then returns the stored code.
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        assert returncode == 0, "an unread child must still exit cleanly"

    asyncio.run(run_child())
