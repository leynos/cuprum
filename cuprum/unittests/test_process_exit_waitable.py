"""Waiting for a child to become collectable without collecting it.

:func:`~cuprum._process_exit._await_child_waitable` waits for one of the states
a delivery decision has to tell apart, without consuming what it waits for. A
double cannot witness it: it has no status to strand, so it can say nothing
about a wait that must leave the status in place.

The cases that must observe a *running* child hold it there rather than
starting one and hoping to be quick. A child that exits promptly leaves the
running state unobserved, so a case built on one cannot distinguish the two
states it claims to separate. The wait is therefore started against a held
child and only released afterwards, so the running state is established by the
case rather than raced.

The neighbouring question — whether the kernel can still be asked about a
child at all — is covered in
:mod:`cuprum.unittests.test_process_exit_state`; the blocking wait itself and
the non-collecting observation are in
:mod:`cuprum.unittests.test_process_exit`.
"""

from __future__ import annotations

import os
import signal
import threading
import typing as typ

from cuprum._process_exit import _await_child_waitable, _observed_exit_code
from cuprum.unittests._process_exit_child_support import (
    _OBSERVE_TIMEOUT,
    _OBSERVED_EXIT_CODE,
    _release_child,
    _skip_if_unobservable_exit,
    _spawn_exiting_child,
    _spawn_sleeping_child,
)

if typ.TYPE_CHECKING:
    import asyncio
    import subprocess

#: How long a case gives a wait that should not return yet to prove it by
#: staying away. Long enough to outlast the dispatch and the syscall itself,
#: and — because the state under observation is held by the case — it is time
#: for an answer to *appear*, not time for a wait to finish.
_NON_BLOCKING_GRACE = 0.2


def _as_process(child: subprocess.Popen[str]) -> asyncio.subprocess.Process:
    """Present a real child to a probe that declares a process it may not modify.

    The probes take ``asyncio.subprocess.Process`` and read only ``pid`` and
    ``returncode`` from it, but ``subprocess.Popen`` is the only way to start
    a child whose waiting the case itself controls. The cast records that
    narrow use rather than widening the probe's declared type to accommodate
    a test.

    Returns
    -------
    asyncio.subprocess.Process
        The same child, presented under the type the probes declare. No
        attribute is copied or adapted, so a probe reading it reaches the real
        process.
    """
    return typ.cast("asyncio.subprocess.Process", child)


def test_await_child_waitable_blocks_then_leaves_the_status() -> None:
    """Waiting for a child must last until it is collectable, and not collect.

    This is what keeps the reaper's locked section to a few instructions: the
    lock is entered only once ``wait4`` has nothing left to wait for, so no
    delivery is ever parked behind a child's lifetime. Two properties have to
    hold together for that to be true, and each would be broken by the obvious
    wrong implementation of the other.

    The wait must actually wait — a probe that answered at once would report a
    running child as collectable, and the collection that followed would block
    inside the locked section, which is the defect the ordering exists to
    avoid. And it must not collect: a probe that consumed the status would
    strand it, and the reaper would find nothing where the child's exit code
    and resource usage should be. A probe that merely answered would satisfy
    the second property while failing the first, so the two are asserted
    against a single held child in the two states a wait has to tell apart.

    The wait is started while the child is still running, which is what makes
    the blocking claim testable. Killing it first would let a probe that never
    waits pass: with the child already a zombie, an immediate answer and a
    blocking one are indistinguishable. So the caller starts the wait against
    a live child, observes that the answer has not arrived, and only then
    releases the child — from another thread, because the waiting thread has
    to stay in the wait for the first observation to mean anything.

    Killing the child first would be the trivial version of this case, and it
    is deliberately not what happens: the child must be running at the moment
    the wait begins.
    """
    _skip_if_unobservable_exit()
    child = _spawn_sleeping_child()
    answers: list[bool] = []

    def wait_in_a_thread() -> None:
        """Record the wait's answer without blocking the observing case."""
        answers.append(_await_child_waitable(child.pid))

    try:
        assert _observed_exit_code(_as_process(child)) is None, (
            "the child must be running when the wait begins, or the case "
            "cannot show the wait was what brought it to collectable"
        )
        waiting = threading.Thread(target=wait_in_a_thread)
        waiting.start()
        # The wait has nothing to collect, so an implementation that returned
        # at once would have answered by now. This is the whole difference
        # between waiting and answering.
        waiting.join(_NON_BLOCKING_GRACE)
        assert waiting.is_alive(), (
            "the wait must block while the child is still running, but it "
            "answered with the child held alive"
        )
        assert not answers, "a running child must not be reported collectable"

        child.kill()
        waiting.join(_OBSERVE_TIMEOUT)
        assert not waiting.is_alive(), "the wait must return once the child has exited"
        assert answers == [True], (
            f"a child that has exited must be reported collectable, got {answers!r}"
        )
        # The wait has returned, and the status must still be there for the
        # reaper that owns it.
        reaped = child.wait()
    finally:
        _release_child(child)
    assert reaped == -signal.SIGKILL, (
        f"waiting for the child must leave its status for the reaper, got {reaped}"
    )


def test_await_child_waitable_reports_a_child_that_is_already_gone() -> None:
    """A child whose status is collected must not be reported collectable.

    The wait is the reaper's route into the locked section, so its answer has
    to be right in the direction that matters most. Reporting ``True`` for a
    child that is gone would carry the reaper into a collection that cannot
    succeed, replacing a recorded status with an invariant failure — and the
    delivery side of the same question would then be asked against a
    half-settled instance.

    A foreign reaper collects the status here, which is the state a completed
    collection leaves behind whatever performed it. The answer must be
    ``False``, and the caller that owns the reap is then free to fall back to
    its own collection and report the mismatch rather than proceeding as if
    the child were ready.
    """
    _skip_if_unobservable_exit()
    child = _spawn_exiting_child(_OBSERVED_EXIT_CODE)
    reaped_pid, status = os.waitpid(child.pid, 0)
    assert reaped_pid == child.pid, "the foreign reaper must collect this child"
    assert os.waitstatus_to_exitcode(status) == _OBSERVED_EXIT_CODE, (
        "the foreign reaper must have taken the real status"
    )
    assert not _await_child_waitable(child.pid), (
        "a child whose status is collected must not be reported collectable"
    )
