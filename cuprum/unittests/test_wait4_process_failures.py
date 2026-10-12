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
import threading
import types

import pytest

from cuprum import _wait4_process
from cuprum._process_exit import _observed_exit_code

_wait4_only = pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="asserts direct-child wait4 process ownership",
)

#: How long a case waits for the state it is observing before reporting that it
#: never arrived, and how long it gives a delivery to appear before closing the
#: window that would hide it. Both are generous because neither is a race: the
#: state each case waits for is held by the case itself, so the durations only
#: have to outlast the dispatch that reaches it. A delivery that reads an
#: unestablished state returns in microseconds, so the second is time for one to
#: *appear*, not time for one to finish.
_OBSERVE_TIMEOUT = 5.0
_WINDOW_OBSERVATION = 0.5


def _skip_if_uncoordinated_reap() -> None:
    """End the case as not applicable where a reap cannot be coordinated.

    The coordination rests on the same kernel question the delivery guard asks,
    so the cases asserting it are meaningful only where that question can be
    answered. The platform is asked directly rather than through the constant
    the code under test exports: that constant is part of what these cases
    judge, so reading it here would let a probe switched off in the code turn
    its own failing case into a skip, and the skip would read as a host
    limitation rather than the defect it is.

    Ends the case through *pytest.skip*, which raises rather than returns, so
    there is nothing to describe under a returns heading.
    """
    probe_platform = (
        os.name == "posix" and hasattr(os, "waitid") and hasattr(os, "CLD_EXITED")
    )
    if not probe_platform:
        pytest.skip("this host cannot coordinate a reap against a delivery")


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
        process = await _wait4_process.spawn_wait4_process(_exiting_config())
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
        # behind. The child exits on its own, so the reaper's wait for it is
        # satisfied before the mismatching ``wait4`` is reached, and a branch
        # that respected the mismatch leaves the status for this probe to
        # collect — which is exactly what ``REAPED`` reports.
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
    assert ownership is _ChildOwnership.REAPED, (
        "the child must have exited with its status left for this probe to "
        f"collect, so the mismatch branch did not reap it; found {ownership!r}"
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


class _ParkedCollection:
    """A ``wait4`` that collects the status, then holds the window open.

    The parking is the point: it lets a case observe the interval in which the
    kernel has taken the child's status but this owner has not yet recorded it,
    without racing the reaper to get there. The call still performs the real
    collection first, so what follows the park is the code under test's own
    publication rather than a stub's.

    The window is closed by :meth:`release`, and it closes on its own after
    :data:`_OBSERVE_TIMEOUT` so a failing case cannot hang the suite on a
    delivery that never arrives.
    """

    def __init__(self) -> None:
        """Start with the window shut and no collection yet observed."""
        self.collected = threading.Event()
        self._release = threading.Event()
        self._real_wait4 = os.wait4

    def __call__(self, pid: int, options: int) -> tuple[int, int, object]:
        """Collect the status, then hold the window open for the case."""
        result = self._real_wait4(pid, options)
        self.collected.set()
        self._release.wait(_OBSERVE_TIMEOUT)
        return result

    def release(self) -> None:
        """Let the parked collection return, publishing what it collected."""
        self._release.set()

    def wait_until_open(self) -> None:
        """Block until the reaper has collected and parked.

        Ends the case with an ``assert`` rather than a raise, so there is
        nothing to describe under a raises heading: a window that never opens
        means the reaper never reached its collection, which is the case
        failing to establish its own precondition rather than the code under
        test misbehaving.
        """
        assert self.collected.wait(_OBSERVE_TIMEOUT), (
            "the reaper must reach the collection for the window to exist"
        )


def _deliver_in_a_thread(
    process: _wait4_process._Wait4Process,
    observed: dict[str, object],
) -> threading.Thread:
    """Attempt a delivery from another thread, recording what it reported.

    The delivery runs off the event loop and off the reaping thread, which is
    the only position from which the coordination is observable: a delivery
    that shares a thread with the reaper cannot demonstrate that one waits for
    the other. The refusal's own error is recorded rather than raised, because
    the point of the case is what the caller could learn afterwards, not that
    the attempt failed.

    Returns
    -------
    threading.Thread
        The delivery, already started. The caller joins it, because whether it
        finished is itself one of the properties under test.
    """

    def deliver() -> None:
        """Record the refusal and the code visible once it returns."""
        try:
            process.send_signal(signal.SIGTERM)
        except ProcessLookupError as exc:
            observed["error"] = exc
        observed["published"] = process.returncode

    delivery = threading.Thread(target=deliver)
    delivery.start()
    return delivery


@_wait4_only
def test_send_signal_waits_for_a_collection_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery arriving during the collection must wait, not slip past.

    This is the defect the coordination closes, driven rather than raced. The
    reaper is parked between the kernel taking the child's status and this
    instance recording it — the interval in which ``returncode`` still reads
    ``None`` for a child that is definitively gone. A delivery checks that
    attribute first, so a delivery that reached the check during the park
    would find nothing to refuse and go on to signal a PID the kernel is free
    to have reused.

    Waiting is what separates the coordinated delivery from the uncoordinated
    one, and it is asserted as a fact about the thread rather than a duration:
    the delivery is still running while the park is held, which no
    lock-free implementation can produce. Once released, the same delivery
    must refuse — the child is gone — so the wait is proven to end in a
    collapse of the window rather than merely an absence.

    A counterfactual confirms the park is load-bearing: with the lock removed
    the delivery completes inside the window instead of waiting, so this case
    fails on the first assertion rather than passing for an unrelated reason.
    """
    _skip_if_uncoordinated_reap()
    parked = _ParkedCollection()
    observed: dict[str, object] = {}

    async def deliver_into_the_window() -> None:
        """Open the window, deliver into it, then let the collection finish."""
        process = await _wait4_process.spawn_wait4_process(_exiting_config())
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        reaping = asyncio.ensure_future(process.wait())
        # Waiting off the loop is what lets the reaping task run: the park is
        # reached by that task, so blocking the loop here would deadlock the
        # case against itself.
        await asyncio.to_thread(parked.wait_until_open)
        assert process.returncode is None, (
            "the window is open only while the code is unrecorded; a recorded "
            "code means the case is not testing the interval it names"
        )

        delivery = _deliver_in_a_thread(process, observed)
        # Time for an uncoordinated delivery to reach the kill. A coordinated
        # one is still blocked on the lock, so this cannot fail by being early;
        # it can only catch a delivery that entered the window.
        await asyncio.sleep(_WINDOW_OBSERVATION)
        observed["waited"] = delivery.is_alive()
        parked.release()
        delivery.join(_OBSERVE_TIMEOUT)
        assert not delivery.is_alive(), "the delivery must not outlive the window"
        await reaping

    monkeypatch.setattr(_wait4_process.os, "wait4", parked)
    try:
        asyncio.run(deliver_into_the_window())
    finally:
        parked.release()

    assert observed["waited"], (
        "a delivery arriving during the collection must wait for the "
        "publication, but it returned while the window was still open"
    )
    assert isinstance(observed["error"], ProcessLookupError), (
        "the delivery must be refused once the collection is published, "
        f"got {observed['error']!r}"
    )
    assert observed["published"] == _EXITED, (
        "the refusal must read the code the reaper published rather than the "
        f"-1 placeholder, got {observed['published']!r}"
    )


@_wait4_only
def test_send_signal_does_not_wait_on_a_published_child() -> None:
    """Once the code is published, a delivery must be refused at once.

    The coordination must cost a delivery nothing when there is nothing to
    coordinate with. The reaper has already finished here, so the lock is free
    and no park is involved: a delivery that still took the slow path — or
    waited for a reap that had already completed — would show up as a
    delivery that hangs against a child that is definitively gone.

    The refusal is asserted to carry the child's own code as well, because the
    two delivery paths must agree: whether the delivery arrives during the
    collection or long after it, the caller must be told the same thing, and a
    published code is what the second path has to read.

    The delivery is joined within the observation timeout, which is what turns
    "did not wait" into a claim the case can actually make. An unjoined thread
    would leave the assertions reading a record the delivery had not yet
    written, so the case would report the wrong thing under load while still
    appearing to pass. A delivery that genuinely took the slow path here —
    waiting for a reap that had already finished — would hang rather than fail,
    so the join is what keeps the regression inside the case that names it.
    """
    _skip_if_uncoordinated_reap()
    observed: dict[str, object] = {}
    delivery: dict[str, threading.Thread] = {}

    async def refuse_after_the_reap() -> None:
        """Signal a child whose reaper has already published its code."""
        process = await _wait4_process.spawn_wait4_process(_exiting_config())
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        await process.wait()
        assert process.returncode == _EXITED, (
            "the reaper must have published the code this case delivers after"
        )
        delivery["thread"] = _deliver_in_a_thread(process, observed)

    asyncio.run(refuse_after_the_reap())

    delivery["thread"].join(_OBSERVE_TIMEOUT)
    assert not delivery["thread"].is_alive(), (
        "a delivery to a published child must be refused without waiting for anything"
    )
    assert isinstance(observed["error"], ProcessLookupError), (
        f"a published child must be refused, got {observed['error']!r}"
    )
    assert observed["published"] == _EXITED, (
        f"the refusal must report the child's own code, got {observed['published']!r}"
    )
