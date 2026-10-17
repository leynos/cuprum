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
import threading
import typing as typ

import pytest

from cuprum.sh import RunOutputOptions, SafeCmd, StdinInput
from tests.helpers.catalogue import python_builder

# How long the grandchild may hold the pipe open. The test finishes long
# before this, so the window is never the constraint; it is here solely so a
# grandchild that is never collected still dies on its own. The child cannot be
# asked to reap what it spawned — it exits immediately, which is the whole
# point of the scenario — and the grandchild is reparented to init, so the
# parent has no handle either. A self-imposed cap is therefore the only bound
# available, and it keeps a run of this suite from accumulating live processes.
_GRANDCHILD_LIFETIME_S = 30

# The child starts a grandchild that holds the inherited stdin pipe open, then
# exits at once. The grandchild reads nothing, so the writer's remaining work
# can reach nobody and has nothing to fail against — there is no EPIPE to wait
# for, only a pipe end that will not close.
#
# The grandchild takes no ``stdin=`` argument, so it inherits the child's
# descriptor as-is; that inherited pipe end is exactly what keeps the parent's
# ``wait_closed`` pending after the child itself is gone.
_GRANDCHILD_HOLDS_STDIN = (
    "import subprocess, sys\n"
    "subprocess.Popen([\n"
    "    sys.executable,\n"
    "    '-c',\n"
    f"    'import time; time.sleep({_GRANDCHILD_LIFETIME_S})',\n"
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


def _within_unwind_window(style: str) -> tuple[bool, object]:
    """Run one style under a join timeout the caller can actually observe.

    The worker is a bare daemon thread rather than a ``ThreadPoolExecutor``.
    A worker in this scenario does not merely finish slowly: the scenario is
    that the writer never unwinds at all, which wedges the worker forever.
    ``ThreadPoolExecutor.__exit__`` calls ``shutdown(wait=True)`` and so joins
    its workers unconditionally, and a ``join(timeout=...)`` that expires does
    not keep the context manager from joining anyway — so the ``with`` block
    hangs exactly where the timeout was meant to keep it from hanging, and the
    ``TimeoutError`` surfaces only once the wedged worker has already been
    reaped. A daemon thread has no such exit hook, so the join below is the
    only wait and its timeout is genuinely decisive.

    Returns
    -------
    tuple[bool, object]
        Whether the worker finished within the window, and its return value
        when it did. An unfinished worker yields ``(False, None)``.

    Whatever ``_run_under_deadline`` raised inside the worker is re-raised
    here, in the test thread. A failure there is otherwise invisible from the
    caller, and it is not the hang this helper exists to detect — reporting it
    as one would misattribute a broken scenario to a wedged writer.
    """
    outcome: list[object] = []
    failures: list[BaseException] = []

    def run_worker() -> None:
        """Record the run's outcome, keeping any failure for the caller."""
        try:
            outcome.append(_run_under_deadline(style))
        except BaseException as exc:
            # Recorded and re-raised rather than swallowed: the caller
            # re-raises this in the test thread, so the real failure reaches
            # pytest. Re-raising here also keeps the raise visible to the
            # linter that requires it in the handler body.
            failures.append(exc)
            raise

    worker = threading.Thread(
        target=run_worker,
        name=f"teardown-bound-{style}",
        daemon=True,
    )
    worker.start()
    worker.join(timeout=_UNWIND_WINDOW_S)
    if worker.is_alive():
        # Nothing is going to fill ``outcome`` now, so say so directly rather
        # than letting the caller index an empty list and report a missing
        # result for what is really a hung run.
        return False, None
    if failures:
        raise failures[0]
    return True, outcome[0]


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
    # A daemon thread plus a join timeout, not a bare call: a regression hangs
    # the writer in an uninterruptible await, and this test must *fail* rather
    # than hang the suite. See ``_within_unwind_window`` for why the executor
    # this replaced could not deliver that.
    finished, outcome = _within_unwind_window(style)

    assert finished, (
        f"the {style} run did not unwind within {_UNWIND_WINDOW_S:.0f}s of "
        "its child exiting; a writer parked in its own teardown has hung the "
        "run rather than merely failed it"
    )
    assert outcome in {0, None}, (
        "the child exited at once, so the run must report its success or hit "
        f"its own deadline; it reported {outcome!r}"
    )
