"""A configured timeout bounds the whole post-exit phase of a streamed run.

A streamed run has two halves: waiting for the child to exit, and everything
after — stopping the idle heartbeat, awaiting the stdin writer, and gathering
the two stream consumers. The first half has always been bounded. The second is
reachable without a deadline of its own, because a child can exit while a
descendant it left behind still holds the run's inherited pipes: the readers
then never see end-of-file, and the writer can park in ``drain`` on a pipe
nobody drains.

The sibling modules cover cancellation at each of those awaits. These cover the
deadline, which is a different mechanism reaching the same place: no
cancellation is sent and no test double drives the timing, so what is asserted
is that the run's own configured timeout ends it. That distinction matters
because a deadline must survive the boundary being cancelled — and because the
run must end by *raising*, not merely by stopping.

The cases come in two policies. Under ``OWN_GROUP`` the expiry's cleanup can
signal the group, which closes the pipes and releases the writer; whether
reconciliation then reaches the writer is not observable from the run's
outcome. ``INHERIT`` is therefore the case that pins it: there is no group
signal to close anything, so the writer stays parked unless reconciliation
cancels it, and the run hangs past its deadline if it does not. Both are run
because the two policies fail differently, and only the pair distinguishes a
boundary that is merely entered from one whose cleanup actually completes.

Each case asserts the descendant is gone as well as the run being over. A fix
that stopped waiting without settling the group would satisfy the timing
assertions alone and still leak the process the policy exists to reclaim.
"""

from __future__ import annotations

import asyncio
import sys
import time
import typing as typ

import pytest

from cuprum.sh import (
    ExecutionContext,
    ProcessGroupPolicy,
    RunOutputOptions,
    StdinInput,
    TimeoutExpired,
)
from cuprum.unittests._post_exit_teardown_support import (
    PARKING_STDIN_BYTES,
    RUN_BUDGET_S,
    SpawnedWriter,
    assert_reclaimed,
    builder,
    owned_scope,
    reap_descendant,
    recorded_descendant,
)
from tests.helpers.timeouts import descendant_then_exit_argv

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import SafeCmd

# The two fixtures are imported rather than defined here; declaring them
# records that pytest resolves these names at collection time rather than
# leaving them to be pruned as unused imports.
__all__ = ["builder", "owned_scope"]

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only

# Long enough that the descendant has certainly installed its SIGTERM handler
# and announced itself before the deadline can fire; the run's own timeout is
# what ends it either way, so this only has to outlast start-up.
_DEADLINE_S = 1.0


def test_a_descendant_holding_the_pipes_does_not_outlive_the_deadline(
    builder: cabc.Callable[..., SafeCmd],
    owned_scope: None,
    tmp_path: Path,
) -> None:
    """A configured timeout covers the stream collection, not just the exit wait.

    The child exits well before the deadline, so the exit wait succeeds and the
    run moves on to collecting its consumers — where a descendant it left
    behind holds the inherited pipes, and the readers therefore never see
    end-of-file. Nothing in that second half used to look at the deadline at
    all, so the timeout measured from the run's start silently covered only its
    first half and the run hung indefinitely: the descendant was never
    signalled, because the cleanup that would signal it is what the hang
    prevented.

    The deadline now bounds the collection as well, and expiry takes the
    ordinary cleanup route — so this also asserts the descendant is gone. A fix
    that merely stopped waiting, without settling the group, would return on
    time and leave the descendant holding the pipes, which is the failure the
    policy exists to prevent.
    """
    pid_file = tmp_path / "post-exit-deadline.pid"
    command = builder(*descendant_then_exit_argv(pid_file, exit_code=0))

    async def run_case() -> None:
        """Run with a deadline the child meets but the collection cannot."""
        descendant_pid: int | None = None
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutExpired):
                await asyncio.wait_for(
                    command.run(
                        output=RunOutputOptions(capture=True, echo=False),
                        timeout=_DEADLINE_S,
                        context=ExecutionContext(
                            process_group=ProcessGroupPolicy.OWN_GROUP,
                        ),
                    ),
                    RUN_BUDGET_S,
                )
            elapsed = time.monotonic() - started
            assert elapsed < RUN_BUDGET_S, (
                "the run reached this assertion too late to have been bounded "
                "by its own deadline"
            )
            descendant_pid = recorded_descendant(pid_file)
            assert descendant_pid is not None, (
                "the child never recorded the descendant it left behind, so "
                "this run did not reach the state under test"
            )
            await assert_reclaimed(
                descendant_pid,
                "a deadline expiring during stream collection must settle the "
                "group its child led, not merely stop waiting on consumers "
                "that can never see end-of-file",
            )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


@pytest.mark.parametrize(
    "policy",
    [ProcessGroupPolicy.OWN_GROUP, ProcessGroupPolicy.INHERIT],
    ids=["own-group", "inherit"],
)
def test_a_descendant_that_never_reads_stdin_does_not_outlive_the_deadline(
    builder: cabc.Callable[..., SafeCmd],
    owned_scope: None,
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    policy: ProcessGroupPolicy,
) -> None:
    """The deadline covers the stdin writer, the await before the gather.

    The child exits before its deadline, leaving a descendant that inherits its
    stdin and never reads it. Nothing drains that pipe, so the run's writer
    parks in ``drain`` on a payload larger than the pipe buffer and never
    finishes. A timeout that began at the gather would not be reached at all —
    the run hangs in the await *before* the bounded one — so this is the case
    that distinguishes a deadline covering the boundary from one covering only
    its last step.

    The failure is asserted as the public ``TimeoutExpired`` rather than as a
    generic ``TimeoutError``, because the internal ``_SubprocessTimeoutError``
    is not one: asserting the internal type would pass on a path that never
    reached the execution boundary that performs the conversion.

    No cancellation is sent: the run is expected to end on its own deadline.

    The policy is what makes this case able to fail. Under ``OWN_GROUP`` the
    expiry's cleanup signals the group, which closes the pipes and releases the
    writer whether or not reconciliation reaches it — so a run that bounded the
    writer await and then never settled the writer still returns on time, and
    an assertion on the writer's settlement passes either way. Under
    ``INHERIT`` nothing closes the pipe: the writer stays parked unless
    reconciliation cancels it, and a cleanup that does not reach the writer
    hangs the run past the deadline it was given. The ``INHERIT`` arm is
    therefore the one that pins the reconciliation, and it is run alongside
    the owned one because the owned arm is what a caller reaching for this
    feature would write first.

    The writer-settled assertion is read against that same asymmetry. It is
    only the ``INHERIT`` arm that can fail it: with the cancellation removed
    from reconciliation, ``[inherit]`` reports the writer still running when
    the run returns while ``[own-group]`` still passes, because the group
    signal released it. That mutation is what the assertion was checked
    against, so it is known to discriminate rather than merely to be true.
    """
    pid_file = tmp_path / f"post-exit-stdin-deadline-{policy.name}.pid"
    command = builder(*descendant_then_exit_argv(pid_file, exit_code=0))
    watch = SpawnedWriter(monkeypatch)

    async def run_case() -> None:
        """Run with a deadline the parked stdin writer cannot meet."""
        descendant_pid: int | None = None
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutExpired):
                await asyncio.wait_for(
                    command.run(
                        output=RunOutputOptions(capture=True, echo=False),
                        timeout=_DEADLINE_S,
                        stdin=StdinInput(data=b"x" * PARKING_STDIN_BYTES),
                        context=ExecutionContext(process_group=policy),
                    ),
                    RUN_BUDGET_S,
                )
            elapsed = time.monotonic() - started
            assert elapsed < RUN_BUDGET_S, (
                "the run reached this assertion too late to have been bounded "
                "by its own deadline"
            )
            assert watch.task is not None, (
                "the run spawned no stdin writer, so the payload was never "
                "handed to a task and the parked-writer case was not reached"
            )
            assert watch.task.done(), (
                "the run returned with its stdin writer still running, so the "
                "expiry's cleanup stopped waiting on the writer without ever "
                "settling it"
            )
            descendant_pid = recorded_descendant(pid_file)
            assert descendant_pid is not None, (
                "the child never recorded the descendant it left behind, so "
                "this run did not reach the state under test"
            )
            if policy is ProcessGroupPolicy.OWN_GROUP:
                # The group signal is what makes this reachable, so it is
                # asserted; under INHERIT the descendant is deliberately left
                # alone and there is nothing to reclaim.
                await assert_reclaimed(
                    descendant_pid,
                    "a deadline expiring on the stdin writer must settle the "
                    "group its child led, not merely stop waiting on a writer "
                    "that can never flush into a pipe nobody reads",
                )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())
