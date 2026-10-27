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
assertions alone and still leak the process the policy exists to reclaim. One
case asserts the raised ``TimeoutExpired`` payload too: both expiry routes end
in the same public exception, so a capturing run's text has to survive this
route as it does the other, or the run reports ``None`` for output it was asked
to capture.
"""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import pytest

from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions, StdinInput
from cuprum.unittests._post_exit_teardown_support import (
    PARKING_STDIN_BYTES,
    SpawnedWriter,
    assert_reclaimed,
    owned_builder,
    reap_descendant,
    recorded_descendant_or_fail,
    run_to_its_own_deadline,
)
from tests.helpers.timeouts import descendant_then_exit_argv

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import SafeCmd

# The fixture is imported rather than defined here; declaring it records that
# pytest resolves the name at collection time rather than leaving it to be
# pruned as an unused import.
__all__ = ["owned_builder"]

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only

# Long enough that the descendant has certainly installed its SIGTERM handler
# and announced itself before the deadline can fire; the run's own timeout is
# what ends it either way, so this only has to outlast start-up.
_DEADLINE_S = 1.0

# What the child announces on stdout before it spawns anything. The expiry is
# expected to report it back, so the assertion is on text only this run could
# have produced.
_CHILD_MARKER = "child-stdout-before-the-deadline"


def test_a_descendant_holding_the_pipes_does_not_outlive_the_deadline(
    owned_builder: cabc.Callable[..., SafeCmd],
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
    command = owned_builder(*descendant_then_exit_argv(pid_file, exit_code=0))

    async def run_case() -> None:
        """Run with a deadline the child meets but the collection cannot."""
        descendant_pid: int | None = None
        try:
            await run_to_its_own_deadline(
                command.run(
                    output=RunOutputOptions(capture=True, echo=False),
                    timeout=_DEADLINE_S,
                    context=ExecutionContext(
                        process_group=ProcessGroupPolicy.OWN_GROUP,
                    ),
                ),
            )
            descendant_pid = recorded_descendant_or_fail(pid_file)
            await assert_reclaimed(
                descendant_pid,
                "a deadline expiring during stream collection must settle the "
                "group its child led, not merely stop waiting on consumers "
                "that can never see end-of-file",
            )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_deadline_expiry_still_reports_what_the_child_printed(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> None:
    """The expiry hands the caller the output the run had already read.

    Both of a streamed run's expiry routes end in the same public
    ``TimeoutExpired``, and a caller cannot tell which one it got, so the two
    must carry the same shape of payload. The exit-wait expiry preserves the
    drained text; the collection expiry has to as well, or a capturing run
    reports ``None`` for output the run was explicitly asked to capture — and
    loses it silently, because the run still *raises* either way.

    The child prints and flushes *before* it spawns the descendant, so the text
    is in the run's capture buffer well before the deadline. That ordering is
    what keeps this a test of the payload and not of how promptly a reader
    happens to reach the pipe: by the time collection overruns, the bytes are
    already read.
    """
    pid_file = tmp_path / "post-exit-deadline-output.pid"
    command = owned_builder(
        *descendant_then_exit_argv(
            pid_file,
            exit_code=0,
            stdout_chunks=(f"{_CHILD_MARKER}\n",),
        ),
    )

    async def run_case() -> None:
        """Run to its deadline and assert the expiry carried the child's text."""
        descendant_pid: int | None = None
        try:
            expired = await run_to_its_own_deadline(
                command.run(
                    output=RunOutputOptions(capture=True, echo=False),
                    timeout=_DEADLINE_S,
                    context=ExecutionContext(
                        process_group=ProcessGroupPolicy.OWN_GROUP,
                    ),
                ),
            )
            assert isinstance(expired.output, str), (
                "a capturing run that expires during stream collection must "
                "report the output its consumers had already read; a ``None`` "
                "payload is indistinguishable from a run that printed nothing; "
                f"got {expired.output!r}"
            )
            assert _CHILD_MARKER in expired.output, (
                "the expiry must carry the text the child printed, not merely "
                f"some non-empty payload; got {expired.output!r}"
            )
            descendant_pid = recorded_descendant_or_fail(pid_file)
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


@pytest.mark.parametrize(
    "policy",
    [ProcessGroupPolicy.OWN_GROUP, ProcessGroupPolicy.INHERIT],
    ids=["own-group", "inherit"],
)
def test_a_descendant_that_never_reads_stdin_does_not_outlive_the_deadline(
    owned_builder: cabc.Callable[..., SafeCmd],
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
    hangs the run past the deadline it was given.

    The writer-settled assertion is read against that same asymmetry: only the
    ``INHERIT`` arm can fail it. With the cancellation removed from
    reconciliation, ``[inherit]`` reports the writer still running when the run
    returns while ``[own-group]`` still passes, because the group signal
    released it. That mutation is what the assertion was checked against, so it
    is known to discriminate rather than merely to be true.
    """
    pid_file = tmp_path / f"post-exit-stdin-deadline-{policy.name}.pid"
    command = owned_builder(*descendant_then_exit_argv(pid_file, exit_code=0))
    watch = SpawnedWriter(monkeypatch)

    async def run_case() -> None:
        """Run with a deadline the parked stdin writer cannot meet."""
        descendant_pid: int | None = None
        try:
            await run_to_its_own_deadline(
                command.run(
                    output=RunOutputOptions(capture=True, echo=False),
                    timeout=_DEADLINE_S,
                    stdin=StdinInput(data=b"x" * PARKING_STDIN_BYTES),
                    context=ExecutionContext(process_group=policy),
                ),
            )
            watch.assert_settled_after_run()
            descendant_pid = recorded_descendant_or_fail(pid_file)
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
