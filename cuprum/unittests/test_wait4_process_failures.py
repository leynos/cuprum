"""The failure paths ``cuprum._wait4_process`` owns but a live run never takes.

A command that runs to completion exercises one branch of this module: spawn,
attach pipes, reap once. The other branches exist only for things going wrong,
and they protect two promises. The first is that ``_Wait4Process._reap`` is the
only reaper, which keeps the child's status and its resource usage attributable
to the command that spawned it. The second is that a caller can always learn
how the child ended: the reap publishes the status from the thread that
collected it, and a delivery that the kernel refuses is reported rather than
swallowed. Nothing a caller can observe through ``CommandResult`` separates
"the child was cleaned up" from "the child leaked", or "the signal was refused"
from "the signal landed", so these paths are driven directly rather than
through a command's result.

Each test spawns a real child rather than a stub, because the guarantees under
test are about process lifetime: the cleanup branch must leave no zombie, the
mismatch branch must not adopt another child's exit status, and the two
signal-reporting branches must agree with what the kernel actually did. A fake
process can stand for none of those.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import os
import signal
import sys
import types

import pytest

from cuprum import _wait4_process
from cuprum._process_exit import _observed_exit_code

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

#: Status the short-lived children below choose for themselves. Non-zero and
#: not a signal's negation, so a reading that guessed or defaulted could not
#: match it.
_EXITED = 21


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


def _exiting_config() -> _wait4_process.DirectProcessConfig:
    """Build spawn inputs for a child that exits at once with ``_EXITED``."""
    return _wait4_process.DirectProcessConfig(
        argv=(sys.executable, "-c", f"import sys; sys.exit({_EXITED})"),
        stdin=None,
        stdout=None,
        stderr=None,
        env=None,
        cwd=None,
    )


@_wait4_only
def test_reap_publishes_the_status_before_it_returns() -> None:
    """The reaping thread publishes the code, so no signal can race the gap.

    ``os.wait4`` collecting the status and the instance recording it are two
    events. If they were separated by an event-loop hop, the window between
    them would leave a child that is reaped while ``returncode`` still reads
    ``None`` — and a signal delivered in that window goes by number to a PID
    the kernel may already have recycled, while the handle cannot report the
    child's ending because it has not recorded one. Publishing from the
    reaping thread removes the hop, so this asserts the reaper's own
    postcondition rather than a timing assumption: when the blocking reap has
    returned, the code is already published.

    The assertion is made through the probe the handle uses, because that is
    the observable the defect would corrupt. A colon is deliberately absent
    from the child's command: ``comm`` in procfs is parenthesized, and the
    probe must not depend on parsing it.
    """
    process = asyncio.run(_wait4_process.spawn_wait4_process(_exiting_config()))
    assert isinstance(process, _wait4_process._Wait4Process), (
        "the POSIX path must yield the wait4-owning process"
    )
    published = process._reap_blocking()
    assert published == _EXITED, (
        f"the reaping thread must report the child's own status {_EXITED}, "
        f"got {published}"
    )
    assert process.returncode == _EXITED, (
        "the status must be published before the reap returns, so a signal "
        f"cannot observe a reaped child with returncode {process.returncode!r}"
    )
    assert _observed_exit_code(process) == _EXITED, (
        "the published code must be what the handle's probe reports"
    )


@_wait4_only
def test_send_signal_reports_a_child_reaped_outside_this_owner() -> None:
    """A refused delivery reaches the caller instead of reading as success.

    ``Popen.send_signal`` absorbs ``ProcessLookupError`` (bpo-40550) because
    the caller merely asked to signal a process. This owner cannot take that
    view: it reports to a handle that has to tell its caller *how* the child
    ended, so a swallowed refusal would make the handle claim a delivery to a
    process that does not exist. The error is asserted here, on the backend
    where the suppression would live, because this is the only place the
    distinction is visible.

    The child is collected by a foreign reaper — ``os.waitpid`` called
    directly, which is Case 2 in ``Popen.send_signal``'s own account — so
    nothing publishes a code onto this instance and the ``_returncode`` guard
    cannot answer for it. That leaves delivery as the only operation that can
    discover the child is gone, which is exactly the situation the suppression
    used to hide. The reaped status is checked independently so the case
    cannot pass by signalling a child that was still alive.
    """
    process = asyncio.run(_wait4_process.spawn_wait4_process(_exiting_config()))
    assert isinstance(process, _wait4_process._Wait4Process), (
        "the POSIX path must yield the wait4-owning process"
    )
    reaped_pid, status = os.waitpid(process.pid, 0)
    assert reaped_pid == process.pid, (
        "the foreign reaper must have collected this case's own child"
    )
    assert os.waitstatus_to_exitcode(status) == _EXITED, (
        "the foreign reaper must have collected the child's real status"
    )
    assert process.returncode is None, (
        "the guard must be unable to answer, or delivery is never reached and "
        "the branch under test stays unexercised"
    )

    with pytest.raises(ProcessLookupError):
        process.send_signal(signal.SIGTERM)


@_wait4_only
def test_send_signal_still_delivers_to_a_live_child() -> None:
    """Un-suppressing the failure must not cost the ordinary delivery.

    The negative case above proves the error is reported; this one proves it
    is reported *only* when the child is gone. A change that raised
    unconditionally would satisfy the first test and break every caller that
    signals a running child, which is the operation the handle exists for.

    The child is signalled and then reaped, and the reported status is
    asserted to be the signal's negation rather than a default: that is what
    separates a delivered ``SIGTERM`` from one that never arrived.
    """

    async def signal_and_reap() -> tuple[int, int]:
        """Signal a live child, then reap it and report how it ended."""
        process = await _wait4_process.spawn_wait4_process(_sleep_config())
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        try:
            process.send_signal(signal.SIGTERM)
            return process.pid, await process.wait()
        finally:
            _abandon(process.pid)

    _, status = asyncio.run(signal_and_reap())

    assert status == -signal.SIGTERM, (
        f"a live child must still be signalled and report {-signal.SIGTERM}, "
        f"got {status}"
    )
