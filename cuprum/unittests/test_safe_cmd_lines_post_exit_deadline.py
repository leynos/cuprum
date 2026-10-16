"""A configured timeout bounds the ``lines()`` post-exit drain.

``SafeCmd.lines()`` does three things once its child has exited, and each can
block indefinitely: stop the idle heartbeat, let the stdin writer finish, and
wait for a stream consumer to reach end-of-file. A descendant that inherits the
run's pipes is what makes the last one unbounded — the child is gone, so no
signal aimed at it reaches the descendant, and no reader ever sees end-of-file.
The streamed ``run()`` boundary wraps that whole phase in the run's remaining
configured deadline; the line-stream drain awaited all three bare. The hang is
also what prevents the reconciliation, so the descendant outlives the run.

The child here exits **successfully and well before the deadline**. That is
load-bearing: a case that let the exit wait expire instead would prove nothing
about the drain, because the expiry would have been reached one phase earlier.
Here the only await that can expire is one of the three this module is about.

Both group policies are exercised, and they fail differently. Under
``OWN_GROUP`` the expiry's teardown can signal the group, closing the pipes and
releasing the consumers; under ``INHERIT`` there is no group to signal, and
nothing but reconciliation can settle them. Only the pair distinguishes a
boundary that is merely *entered* from one whose cleanup actually completes.

Every case also asserts the raised exception is the public ``TimeoutExpired``
rather than any ``TimeoutError``. The distinction is not pedantic: the internal
``_SubprocessTimeoutError`` does not subclass the public type, so asserting the
public one is asserting the run reached the execution boundary that performs the
translation — not merely that something timed out along the way.

The stream is drained by a dedicated reader task rather than by the case's own
iteration, because ``lines()`` applies caller backpressure through a finite
queue: a case that stopped iterating would park the consumers on ``queue.put``
and measure that instead of the run. The reader consumes continuously, so the
only thing that can hold the run open is the pipe the descendant retains.
"""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import pytest

from cuprum.sh import (
    ExecutionContext,
    ProcessGroupPolicy,
    RunOutputOptions,
    SafeCmd,
    StdinInput,
    TimeoutExpired,
)
from cuprum.unittests._post_exit_teardown_support import (
    RUN_BUDGET_S,
    assert_reclaimed,
    owned_builder,
    reap_descendant,
    recorded_descendant_or_fail,
    run_to_its_own_deadline,
)
from tests.helpers.timeouts import (
    descendant_then_exit_argv,
    pending_tasks,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.lines import LineEvent

# The fixture is imported rather than defined here; declaring it records that
# pytest resolves the name at collection time rather than leaving it to be
# pruned as an unused import.
__all__ = ["owned_builder"]

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only

# Outlasts the descendant's start-up so it has certainly announced itself, and
# is still far shorter than the budget this module's own bound gives the run.
_DEADLINE_S = 1.0

# Printed by the child before it spawns its descendant, so a case can assert the
# text the run had already delivered survives the expiry.
_MARKER = "before-the-descendant"

# Enough to reach the child, but far below the pipe buffer: the parked writer is
# not this module's subject, and a payload that blocked would put the drain's
# writer await and its consumer await on the same expiry.
_STDIN_BYTES = 64

_POLICIES = [ProcessGroupPolicy.OWN_GROUP, ProcessGroupPolicy.INHERIT]
_POLICY_IDS = ["own-group", "inherit"]


def _lines_kwargs(policy: ProcessGroupPolicy) -> dict[str, object]:
    """Build the keyword arguments every line-stream invocation shares."""
    return {
        "output": RunOutputOptions(capture=True, echo=False),
        "timeout": _DEADLINE_S,
        "stdin": StdinInput(data=b"x" * _STDIN_BYTES),
        "context": ExecutionContext(process_group=policy),
    }


class _DrainedLines:
    """The lines a reader task received, and how its run ended.

    The two are one record because they are only read together and they come
    from one run: the expiry is what the case asserts on, and the delivered
    lines are what show the expiry did not cost the caller text already
    published. Splitting them across two return values would let a case read
    one from a run and the other from a different one.
    """

    def __init__(self, expired: TimeoutExpired, delivered: list[str]) -> None:
        """Record one run's outcome and the lines its reader received."""
        self.expired = expired
        self.delivered = delivered

    @property
    def joined(self) -> str:
        """Every delivered line as one string, for a membership check."""
        return "".join(self.delivered)


async def _drain_lines(lines: cabc.AsyncIterator[LineEvent], seen: list[str]) -> None:
    """Consume *lines* to completion, appending each event's text to *seen*.

    Iterating on a task rather than in the case's own body is what keeps the
    measurement honest. ``lines()`` decouples the caller through a finite
    queue, so a caller that pauses stops the consumers reading the pipe; a case
    that iterated inline would be measuring its own scheduling as much as the
    run's deadline.

    The caller supplies the list rather than receiving one, because the reader
    does not always finish normally: when the run expires, the ``TimeoutExpired``
    propagates out of the iteration and ends this task with that exception. The
    lines it had already delivered are the evidence that the expiry did not cost
    the caller text already published, so they have to outlive the task's own
    outcome.
    """
    # Appended one at a time rather than collected by an async comprehension:
    # the reader ends by *propagating* the run's expiry, and a comprehension
    # builds its whole list before extending, so the lines already delivered
    # would be discarded with it. Those partial lines are the evidence this
    # module asserts on, so the accumulation has to survive the exception.
    async for event in lines:
        seen.append(event.text)  # ruff: ignore[manual-list-comprehension] - a comprehension would lose the partial result the expiry case asserts on


async def _run_to_deadline(
    command: SafeCmd,
    policy: ProcessGroupPolicy,
) -> _DrainedLines:
    """Drive one ``lines()`` run to its own deadline, draining on a reader task.

    The reader is always cancelled and awaited before this returns, so a run
    that was torn down with its consumers still parked cannot hang the case on
    the defect it is testing. What the reader collected before that is read from
    the list the reader appended to, which survives however the task ended —
    normally, cancelled, or by propagating the run's own ``TimeoutExpired``.

    Returns
    -------
    _DrainedLines
        The expiry the run reported, and the lines delivered before it.
    """
    lines = command.lines(**_lines_kwargs(policy))
    delivered: list[str] = []
    reader = asyncio.ensure_future(_drain_lines(lines, delivered))
    try:
        expired = await run_to_its_own_deadline(reader)
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    return _DrainedLines(expired, delivered)


@pytest.mark.parametrize("policy", _POLICIES, ids=_POLICY_IDS)
def test_a_descendant_holding_the_pipes_does_not_outlive_the_lines_deadline(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    *,
    policy: ProcessGroupPolicy,
) -> None:
    """The line-stream post-exit drain honours the configured deadline.

    The child exits before the deadline leaving a descendant that inherited its
    pipes. Nothing closes them, so no consumer reaches end-of-file and the drain
    waits for a reader that can never finish. The line-stream drain awaited that
    with no bound at all, so the run hung past a deadline it had already
    reached — and because the cleanup that reclaims the descendant is what the
    hang prevented, the descendant outlived the run too.

    ``INHERIT`` is the arm that pins the mechanism. Under ``OWN_GROUP`` the
    expiry's teardown can signal the group and close the pipes, which releases
    the consumers whether or not reconciliation ever cancelled them; under
    ``INHERIT`` nothing but reconciliation can, so a fix that bounded the wait
    without settling what it stopped waiting for fails here and nowhere else.

    The marker the child printed before spawning its descendant is asserted to
    survive: a run that reached its deadline is still owed the text it had
    already delivered, and discarding it would make the bound a data-loss bug
    rather than a safety one.
    """
    pid_file = tmp_path / f"lines-{policy.name}.pid"
    command = owned_builder(
        *descendant_then_exit_argv(pid_file, exit_code=0, stdout_marker=_MARKER)
    )

    async def run_case() -> _DrainedLines:
        """Run to its own deadline and report what the run left behind."""
        descendant_pid: int | None = None
        try:
            drained = await _run_to_deadline(command, policy)
            descendant_pid = recorded_descendant_or_fail(pid_file)
            return drained
        finally:
            reap_descendant(pid_file, descendant_pid)

    drained = asyncio.run(run_case())

    assert drained.expired.timeout == _DEADLINE_S, (
        "the expiry must report the timeout the caller configured; a deadline "
        "reported as the remainder it had left would make one field mean two "
        f"things on two routes; got {drained.expired.timeout!r}"
    )
    assert _MARKER in drained.joined, (
        "the lines the child delivered before the deadline must survive the "
        f"expiry, got {drained.delivered!r}"
    )


@pytest.mark.parametrize("policy", _POLICIES, ids=_POLICY_IDS)
def test_the_lines_deadline_settles_the_group_and_leaves_nothing_pending(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    *,
    policy: ProcessGroupPolicy,
) -> None:
    """The bound reclaims what it stopped waiting for.

    A fix that bounded the drain without settling the group would satisfy the
    timing assertion and still strand the descendant — the very outcome the
    issue is about, since the descendant holds the pipes the run's consumers
    were reading. The reclamation is asserted where a group exists to signal,
    and the pending-task check runs on every arm, so a run that returned having
    quietly dropped its consumers or its writer is reported here.

    The task check reads the loop the case owns, after the run has unwound, so
    anything it finds is a leak the run left rather than one the reader task
    still holds: that reader has been cancelled and awaited by this point.
    """
    pid_file = tmp_path / f"lines-settled-{policy.name}.pid"
    command = owned_builder(*descendant_then_exit_argv(pid_file, exit_code=0))

    async def run_case() -> tuple[int, set[asyncio.Task[object]]]:
        """Run to its own deadline, returning the descendant and the loop's tasks."""
        descendant_pid: int | None = None
        try:
            await _run_to_deadline(command, policy)
            descendant_pid = recorded_descendant_or_fail(pid_file)
            if policy is ProcessGroupPolicy.OWN_GROUP:
                await assert_reclaimed(
                    descendant_pid,
                    "the run reached its own deadline with the descendant it had "
                    "left holding its pipes still alive, so the expiry bounded "
                    "the drain without settling the group the descendant was in",
                )
            return descendant_pid, pending_tasks()
        finally:
            reap_descendant(pid_file, descendant_pid)

    descendant_pid, still_pending = asyncio.run(run_case())

    assert descendant_pid > 0, "the child must have recorded a live descendant"
    assert not still_pending, (
        "the line stream returned on its deadline while tasks it owned were "
        "still pending, so the bound stopped waiting without reconciling what "
        f"it stopped waiting for: {still_pending!r}"
    )


@pytest.mark.parametrize("policy", _POLICIES, ids=_POLICY_IDS)
def test_the_lines_deadline_expiry_is_the_public_timeout_expired(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    *,
    policy: ProcessGroupPolicy,
) -> None:
    """The bounded drain is reached on the run's own path, not bypassed.

    A bound that is never reached is indistinguishable from no bound at all
    until the case that reaches it runs. This repeats the parked-consumer
    scenario without instrumenting anything, so the path exercised is exactly
    the one a caller takes, and asserts the failure arrives as the public
    ``TimeoutExpired`` rather than as some internal timeout along the way.

    The case's own bound is asserted to be the looser of the two, so a run
    bounded by the test could never be read as one bounded by its deadline.
    """
    pid_file = tmp_path / f"lines-public-{policy.name}.pid"
    command = owned_builder(*descendant_then_exit_argv(pid_file, exit_code=0))

    async def run_case() -> TimeoutExpired:
        """Run to its own deadline and report the exception the caller met."""
        descendant_pid: int | None = None
        try:
            drained = await _run_to_deadline(command, policy)
            descendant_pid = recorded_descendant_or_fail(pid_file)
            return drained.expired
        finally:
            reap_descendant(pid_file, descendant_pid)

    expired = asyncio.run(run_case())

    assert isinstance(expired, TimeoutExpired), (
        "a line stream that reached its deadline must report the public "
        f"TimeoutExpired, got {type(expired).__name__}"
    )
    assert RUN_BUDGET_S > _DEADLINE_S, (
        "this module's own bound must be the looser of the two, or a run "
        "bounded by the test would be indistinguishable from one bounded by "
        "its own deadline"
    )
