"""The stdin writer's teardown must not outlive the run that requested it.

``stdin.close()`` and ``stdin.wait_closed()`` do not merely flush what cuprum
wrote. They are a graceful shutdown of the whole pipe, and CPython's
``StreamWriter.wait_closed`` waits for the protocol's ``connection_lost``
callback — which the child-side pipe end must reach EOF for. When a grandchild
has inherited the child's stdin read end, that pipe stays open past the child's
own exit, so the callback never fires and the wait never returns.

That matters because ``wait_closed`` runs in the writer's ``finally``. A
cancellation delivered to a task inside a ``finally`` does not skip the
``await``: it schedules one more cancellation, so ``close()`` still runs and
the ``await`` still blocks. Cancelling the writer therefore cannot reclaim it,
and a caller who supplied a payload larger than the pipe buffer waits out a
grandchild it never knew about — past its own deadline, with no error and no
exit.
"""

from __future__ import annotations

import asyncio
import typing as typ
from concurrent.futures import ThreadPoolExecutor

import pytest

from cuprum.sh import RunOutputOptions, SafeCmd, StdinInput
from tests.helpers.catalogue import python_builder

# The child starts a grandchild that holds the inherited stdin pipe open, then
# exits at once. The grandchild reads nothing, so the writer's remaining work
# can reach nobody and has nothing to fail against — there is no EPIPE to wait
# for, only a pipe end that will not close.
_GRANDCHILD_HOLDS_STDIN = (
    "import subprocess, sys\n"
    "subprocess.Popen([\n"
    "    sys.executable,\n"
    "    '-c',\n"
    "    'import time; time.sleep(600)',\n"
    "])\n"
)

# Comfortably past the 64 KiB pipe buffer: the write cannot complete on its
# own, so the writer is genuinely mid-transfer when the deadline arrives.
_BLOCKING_PAYLOAD_BYTES = 1 << 20

# The run's own deadline, and the longest the whole call may take. The gap
# between them is the settle window plus the writer's bounded teardown; it is
# generous so a loaded machine cannot turn scheduling delay into a failure.
_RUN_DEADLINE_S = 1.0
_UNWIND_WINDOW_S = 20.0

# Every public entry point that drives a run.
_EXECUTION_STYLES = ("run", "run_sync")


def _run_under_deadline(style: str) -> int | None:
    """Run the grandchild scenario, letting a deadline bound the run.

    Returns
    -------
    int | None
        The child's exit code, or ``None`` when the deadline expired first.
    """
    command: SafeCmd = python_builder()("-c", _GRANDCHILD_HOLDS_STDIN)
    kwargs: dict[str, typ.Any] = {
        "stdin": StdinInput("x" * _BLOCKING_PAYLOAD_BYTES),
        "output": RunOutputOptions(capture=False),
        "timeout": _RUN_DEADLINE_S,
    }
    try:
        if style == "run_sync":
            return command.run_sync(**kwargs).exit_code
        return asyncio.run(command.run(**kwargs)).exit_code
    except TimeoutError:
        return None


@pytest.mark.parametrize("style", _EXECUTION_STYLES)
def test_a_run_a_grandchild_outlives_still_unwinds(style: str) -> None:
    """The run unwinds once its child exits, however its writer is parked.

    The child exits at once, so the run ought to finish almost immediately and
    long before its own deadline — the grandchild holds only the stdin *read*
    end, which the run's exit path does not wait on. Before the close was
    bounded the call never returned at all: the writer sat in the ``finally``
    of its own task, and neither the exit path nor the deadline could reclaim
    it. ``None`` is accepted because a genuinely loaded machine may reach the
    deadline first, and that is a correct outcome rather than a hang; what the
    test forbids is not returning.
    """
    # A thread plus a join timeout, not a bare call: a regression hangs the
    # writer in an uninterruptible await, and this test must fail rather than
    # hang the suite. ``ThreadPoolExecutor`` threads are daemonized, so one
    # left stuck cannot keep pytest alive.
    with ThreadPoolExecutor(max_workers=1) as pool:
        outcome = pool.submit(_run_under_deadline, style).result(
            timeout=_UNWIND_WINDOW_S
        )

    assert outcome in {0, None}, (
        "the child exited at once, so the run must report its success or hit "
        f"its own deadline; it reported {outcome!r}"
    )
