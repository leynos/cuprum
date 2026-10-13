"""Regression coverage for resilient subprocess exit waiting.

Two readings are covered here, and they answer different questions.
:func:`~cuprum._process_exit._await_process_exit` blocks until a child is gone,
and the cases for it use a double because the failure it guards against — a
waiter that strands after the code is already published — cannot be asked of a
real child. :func:`~cuprum._process_exit._observed_exit_code` only *looks*, and
its cases use a real child, because what it must not do is collect the status
it observes. A double cannot witness that: it has no status to strand.

The two further readings are each a question about a child's state rather than
its outcome, and neither can be answered by a double either: whether the kernel
can still be asked about a child at all is covered in
:mod:`cuprum.unittests.test_process_exit_state`, and waiting for a child to
become collectable in :mod:`cuprum.unittests.test_process_exit_waitable`.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._process_exit import (
    _PROCESS_EXIT_INITIAL_POLL_INTERVAL,
    _PROCESS_EXIT_MAX_POLL_INTERVAL,
    _await_process_exit,
    _observed_exit_code,
)
from cuprum.unittests._process_exit_child_support import (
    _OBSERVED_EXIT_CODE,
    _PublishedExitProcess,
    _skip_if_unobservable_exit,
    _spawn_exiting_child,
    _StrandedWaitProcess,
    _wait_until_exited,
)

if typ.TYPE_CHECKING:
    import pytest


def test_await_process_exit_accepts_published_returncode() -> None:
    """A published return code wins over an orphan pending waiter."""

    async def run_case() -> int:
        """Wait for the stranded process double without hanging."""
        process = typ.cast("asyncio.subprocess.Process", _PublishedExitProcess())
        return await asyncio.wait_for(_await_process_exit(process), timeout=0.5)

    assert asyncio.run(run_case()) == 0, (
        "a published exit code must complete a stranded process waiter"
    )


def test_await_process_exit_backs_off_while_waiter_is_stranded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lost-wakeup fallback backs off and cancels its losing waiter."""

    async def run_case() -> tuple[int, list[float], bool]:
        """Publish an exit code after enough polls to reach the backoff cap."""
        process = _StrandedWaitProcess()
        intervals: list[float] = []
        original_sleep = asyncio.sleep

        async def record_sleep(interval: float) -> None:
            """Record each delay before eventually publishing the exit code."""
            intervals.append(interval)
            if len(intervals) == 10:
                process.returncode = 0
            await original_sleep(0)

        monkeypatch.setattr("cuprum._process_exit.sleep", record_sleep)
        exit_code = await _await_process_exit(
            typ.cast("asyncio.subprocess.Process", process)
        )
        return exit_code, intervals, process.wait_cancelled

    exit_code, intervals, wait_cancelled = asyncio.run(run_case())

    assert exit_code == 0, "the published exit code must complete the fallback"
    assert intervals == [
        min(
            _PROCESS_EXIT_INITIAL_POLL_INTERVAL * 2**index,
            _PROCESS_EXIT_MAX_POLL_INTERVAL,
        )
        for index in range(10)
    ], "long-lived waiters must use capped exponential backoff"
    assert wait_cancelled, "completion must cancel the stranded process waiter"


def test_observed_exit_code_reads_a_child_no_wait_has_reaped() -> None:
    """A merely-held child's status is observable without collecting it.

    This is the reading a terminal refusal depends on. A handle that holds a
    child no wait has reaped sees ``returncode`` as ``None`` however long ago
    the child exited, so a refusal that consulted only that attribute would
    report the placeholder ``-1`` — or, worse, find nothing to refuse and
    deliver a signal into a child that is already gone.
    """
    _skip_if_unobservable_exit()
    child = _spawn_exiting_child(_OBSERVED_EXIT_CODE)
    try:
        _wait_until_exited(child.pid)
        # The case is only meaningful while the child is unreaped: were it
        # collected, ``returncode`` would carry the status and a reading that
        # consulted it would be indistinguishable from one that did not. This
        # is also what keeps the case honest against a later ``_wait_until_exited``
        # that merely times out on a running child.
        assert child.returncode is None, (
            "the child must still be unreaped when the probe reads it"
        )
        observed = _observed_exit_code(typ.cast("asyncio.subprocess.Process", child))
    finally:
        child.wait()
    assert observed == _OBSERVED_EXIT_CODE, (
        f"an exited unreaped child must report its own status "
        f"{_OBSERVED_EXIT_CODE}, got {observed}"
    )


def test_observed_exit_code_leaves_the_status_for_the_reaper() -> None:
    """The probe must not consume the status its owner still has to collect.

    Reading a child's exit is only half the job: the reading is worthless if it
    collects the status as a side effect, because the run's own reaper then
    finds nothing to record and the handle settles with no exit code at all.
    ``WNOWAIT`` is what makes the difference, and this is the case that would
    notice if it were dropped — the observed value would be right while the
    collected one turned into ``ChildProcessError``.
    """
    _skip_if_unobservable_exit()
    child = _spawn_exiting_child(_OBSERVED_EXIT_CODE)
    try:
        _wait_until_exited(child.pid)
        assert child.returncode is None, (
            "the child must still be unreaped when the probe reads it"
        )
        observed = _observed_exit_code(typ.cast("asyncio.subprocess.Process", child))
        reaped = child.wait()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert observed == _OBSERVED_EXIT_CODE, "the probe must read the child's status"
    assert reaped == _OBSERVED_EXIT_CODE, (
        "observing an exit must leave the status for the reaper to collect, "
        f"got {reaped}"
    )
