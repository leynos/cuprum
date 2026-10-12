"""What the ``wait4`` owner guarantees when it spawns or reaps a child.

``_Wait4Process`` exists so that the child's exit status and its resource usage
come from one place. Three promises follow from that, and none of them can be
observed through a command's result: a spawn that fails partway leaves no
child behind, a reap that reports a child this owner did not spawn becomes a
failure rather than a silent attribution, and the status is published before
the reaping call returns so no later reader can see a collected child with an
unrecorded code.

Each case spawns a real child rather than a stub, because every one of those
promises is about what the kernel did with a process. The children, spawn
inputs, and process probes live in
:mod:`cuprum.unittests._wait4_process_support`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import typing as typ

import pytest

from cuprum import _wait4_process
from cuprum._pipeline_types import _ExecutionInvariantError
from cuprum._process_exit import _observed_exit_code
from cuprum._wait4_process_errors import _Wait4InvariantError
from cuprum.unittests._wait4_process_support import (
    _EXITED,
    _abandon,
    _ChildOwnership,
    _exiting_config,
    _FakeUsage,
    _probe_child,
    _sleep_config,
    _wait4_only,
)

if typ.TYPE_CHECKING:
    import types


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
        _Wait4InvariantError,
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
        with pytest.raises(_Wait4InvariantError) as caught:
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

    assert isinstance(failure, _Wait4InvariantError), (
        f"a mismatched reap must raise the wait4 invariant error, got {failure!r}"
    )
    assert failure.expected_pid == process.pid, (
        "the error must name the pid this owner spawned"
    )
    assert failure.reaped_pid == process.pid + 1, (
        "the error must name the pid wait4 actually reported"
    )
    assert isinstance(failure, _ExecutionInvariantError), (
        "the error must stay inside the internal invariant hierarchy"
    )
    assert process.returncode is None, (
        "a rejected reap must not publish an exit code it did not establish"
    )
    assert process.resource_usage is None, (
        "a rejected reap must not publish another child's resource usage"
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
