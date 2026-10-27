"""A configured timeout bounds the direct run's post-exit stdin wait.

A *direct* run — one that captures and echoes nothing — has exactly one thing
to do after its child exits: reconcile the stdin writer. The streamed run next
door wraps that whole phase in the run's remaining deadline; the direct run
awaited its writer bare. That single unbounded await is enough to hang the run
indefinitely, because the writer can park where nothing will release it: the
child exits leaving a descendant that inherited the run's stdin and never
reads it, so the writer blocks in ``drain`` on a payload larger than the pipe
buffer. The await before the cleanup is unreachable, so the cleanup never runs,
so the descendant is never signalled — the same self-blocking shape the
streamed boundary was fixed for.

The child here exits **successfully and well before the deadline**. That is
load-bearing: the exit wait is what expired in the streamed path's existing
tests, and a case that also expired there would prove nothing about the code
under change. Here expiry can only have come from the writer await.

Both group policies are exercised, and they fail differently. Under
``OWN_GROUP`` the expiry's teardown can signal the group, which closes the pipe
and releases the writer; under ``INHERIT`` there is no group to signal and
nothing but reconciliation can settle the writer. Only the pair distinguishes a
boundary that is merely *entered* from one whose cleanup actually completes.

All four entrypoints that reach the direct path are exercised — ``run``,
``run_bytes``, ``run_sync`` and ``run_bytes_sync``. They do not all carry the
same weight, and saying so is more useful than implying they do. The two async
ones run on the loop the case itself owns, so the "nothing left pending"
assertion is made against a loop that is still running: for them, a writer that
was never cancelled is *observed* as a leak. The two synchronous ones delegate
to ``asyncio.run``, which closes its loop by cancelling whatever is still
pending, so that assertion cannot see a stranded writer there — asyncio's
shutdown disposes of it first. What the sync arms still establish is real but
narrower: that the wrapper reaches the same bounded path, reports the same
public ``TimeoutExpired``, and settles the group. Reaching the path through
``run`` alone would leave that much unchecked, which is why they are here, but
no arm of this module should be read as pinning cancellation of the writer on
a synchronous entrypoint.

Each case asserts the raised exception is the public ``TimeoutExpired`` rather
than any ``TimeoutError``. The distinction is not pedantic: the internal
``_SubprocessTimeoutError`` is not a subclass of the public type, so asserting
the public one is asserting that the run reached the execution boundary that
performs the translation — not merely that something timed out along the way.

Every case also asserts nothing is left pending on the loop. A fix that bounded
the writer await without settling the writer would satisfy the timing assertion
and still leak the task it stopped waiting for — on the async entrypoints, where
that assertion has something to see.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import sys
import typing as typ

import pytest

from cuprum._process_group import _POST_KILL_SETTLEMENT_S
from cuprum._subprocess_stdin_reclaim import _STDIN_RECLAIM_BUDGET_S
from cuprum.sh import (
    ExecutionContext,
    ProcessGroupPolicy,
    RunOutputOptions,
    SafeCmd,
    StdinInput,
)
from cuprum.unittests._post_exit_teardown_support import (
    PARKING_STDIN_BYTES,
    SpawnedWriter,
    assert_reclaimed,
    owned_builder,
    reap_descendant,
    recorded_descendant_or_fail,
    run_to_its_own_deadline,
)
from tests.helpers.timeouts import (
    descendant_then_exit_argv,
    pending_tasks,
    process_is_running,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

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
# is still far shorter than the budget the test's own bound gives the run.
_DEADLINE_S = 1.0

# The direct run's own mode: nothing captured, nothing echoed, no sink, no line
# hook, no observers. That is precisely the configuration that takes the path
# under test — a run that captured or echoed would take the streamed one.
_DIRECT_OUTPUT = RunOutputOptions(capture=False, echo=False)

_POLICIES = [ProcessGroupPolicy.OWN_GROUP, ProcessGroupPolicy.INHERIT]
_POLICY_IDS = ["own-group", "inherit"]

# What one entrypoint's invocation looks like to a test: the policy is already
# bound, so hand it the command and get back something
# ``run_to_its_own_deadline`` can await.
_Invoke = cabc.Callable[
    [SafeCmd],
    cabc.Coroutine[object, object, object],
]


def _direct_kwargs(policy: ProcessGroupPolicy) -> dict[str, object]:
    """Build the keyword arguments every direct-path invocation shares."""
    return {
        "output": _DIRECT_OUTPUT,
        "timeout": _DEADLINE_S,
        "stdin": StdinInput(data=b"x" * PARKING_STDIN_BYTES),
        "context": ExecutionContext(process_group=policy),
    }


def _invoke_async(entrypoint: str, policy: ProcessGroupPolicy) -> _Invoke:
    """Return an invoker for an asynchronous entrypoint named by attribute.

    Returns
    -------
    _Invoke
        A callable taking the command and returning the run's coroutine.
    """

    def invoke(command: SafeCmd) -> cabc.Coroutine[object, object, object]:
        """Call *entrypoint* on *command* under this case's direct-path kwargs."""
        coroutine: cabc.Coroutine[object, object, object] = getattr(
            command, entrypoint
        )(**_direct_kwargs(policy))
        return coroutine

    return invoke


def _invoke_sync(entrypoint: str, policy: ProcessGroupPolicy) -> _Invoke:
    """Return an invoker for a blocking entrypoint, run off the loop's thread.

    The synchronous entrypoints block, so each is handed to a worker thread and
    awaited there. What matters is that the run reaches the direct path through
    the wrapper rather than through ``run``; which thread drives the loop the
    wrapper opens is not the claim under test, and the loop-bound assertions the
    case makes are on the thread that ran the case.

    Returns
    -------
    _Invoke
        A callable taking the command and returning an awaitable that performs
        the blocking run on a worker thread.
    """

    def invoke(command: SafeCmd) -> cabc.Coroutine[object, object, object]:
        """Return the blocking call, wrapped for the loop that awaits it."""

        async def call() -> object:
            """Run the blocking entrypoint without stalling the event loop."""
            return await asyncio.to_thread(
                getattr(command, entrypoint),
                **_direct_kwargs(policy),
            )

        return call()

    return invoke


# Every entrypoint that reaches the direct path, with the name a reader needs to
# read a failing parametrization. They are built by attribute rather than
# written out one call at a time so that adding an entrypoint is a row here
# rather than a fifth near-identical function; a signature change to any of them
# still fails the affected case, because the attribute is resolved when the case
# runs and the parametrization names all four.
_ENTRYPOINTS: list[tuple[str, cabc.Callable[[ProcessGroupPolicy], _Invoke]]] = [
    ("run", lambda policy: _invoke_async("run", policy)),
    ("run-bytes", lambda policy: _invoke_async("run_bytes", policy)),
    ("run-sync", lambda policy: _invoke_sync("run_sync", policy)),
    ("run-bytes-sync", lambda policy: _invoke_sync("run_bytes_sync", policy)),
]


# One case per (entrypoint, policy), as a pair rather than as two stacked
# parametrize marks: the case's body reads the policy and the entrypoint
# together, and naming the pair keeps the dependency visible in the id.
_CASES: list[tuple[str, ProcessGroupPolicy]] = [
    (name, policy) for name in dict(_ENTRYPOINTS) for policy in _POLICIES
]
_CASE_IDS = [
    f"{name}-{policy.name.lower().replace('_', '-')}" for name, policy in _CASES
]


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_a_descendant_holding_stdin_does_not_outlive_the_direct_run_deadline(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, ProcessGroupPolicy],
) -> None:
    """The direct run's post-exit stdin await honours the configured deadline.

    The child exits before the deadline leaving a descendant that inherited its
    stdin. Nothing reads that pipe, so the run's writer parks on a payload
    larger than the pipe buffer. The direct path awaited it with no bound at
    all, so the run hung past a deadline that had already been reached and
    spent — and because the cleanup that settles the writer is what the hang
    prevented, the descendant outlived the run too.

    ``INHERIT`` is the arm that pins the mechanism. Under ``OWN_GROUP`` the
    expiry's teardown can signal the group and close the pipe, which releases
    the writer whether or not reconciliation ever cancelled it; under
    ``INHERIT`` nothing but reconciliation can, so a fix that bounded the await
    without settling what it stopped waiting for fails here and nowhere else.
    Both run because only the pair separates a bound that was entered from a
    cleanup that completed.
    """
    entrypoint, policy = case
    pid_file = tmp_path / f"direct-stdin-{entrypoint}-{policy.name}.pid"
    command = owned_builder(*descendant_then_exit_argv(pid_file, exit_code=0))
    watch = SpawnedWriter(monkeypatch)
    invoke = dict(_ENTRYPOINTS)[entrypoint](policy)

    async def run_case() -> None:
        """Run to its own deadline and assert nothing the run owned survived."""
        descendant_pid: int | None = None
        try:
            await run_to_its_own_deadline(
                invoke(command),
                cleanup_allowance_s=_STDIN_RECLAIM_BUDGET_S
                + (
                    ExecutionContext().cancel_grace + _POST_KILL_SETTLEMENT_S
                    if policy is ProcessGroupPolicy.OWN_GROUP
                    else 0
                ),
            )
            watch.assert_settled_after_run()
            descendant_pid = recorded_descendant_or_fail(pid_file)
            if policy is ProcessGroupPolicy.OWN_GROUP:
                await assert_reclaimed(
                    descendant_pid,
                    "the run reached its own deadline with the descendant it "
                    "had left holding its stdin still alive, so the expiry "
                    "settled the writer without settling the group the "
                    "descendant was in",
                )
            else:
                assert process_is_running(descendant_pid), (
                    "an inherited run must leave its descendant alive"
                )
            assert not pending_tasks(), (
                "the direct run returned on its deadline while tasks it owned "
                "were still pending, so the bound stopped waiting without "
                "reconciling what it stopped waiting for"
            )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


@pytest.mark.parametrize("policy", _POLICIES, ids=_POLICY_IDS)
def test_the_direct_run_deadline_expiry_is_the_public_timeout_expired(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    *,
    policy: ProcessGroupPolicy,
) -> None:
    """The bounded await is reached on the run's own path, not bypassed.

    A bound that is never reached is indistinguishable from no bound at all
    until the case that reaches it is run. This repeats the parked-writer
    scenario without instrumenting anything, so the path exercised is exactly
    the one a caller takes, and asserts the exception is the public
    ``TimeoutExpired`` carrying the configured timeout rather than the shortened
    remainder: a caller cannot tell which route expired, so the two expiry
    routes have to report the same value.
    """
    pid_file = tmp_path / f"direct-public-{policy.name}.pid"
    command = owned_builder(*descendant_then_exit_argv(pid_file, exit_code=0))

    async def run_case() -> None:
        """Run to its own deadline and assert the public contract it reported."""
        descendant_pid: int | None = None
        try:
            expired = await run_to_its_own_deadline(
                _invoke_async("run", policy)(command),
                cleanup_allowance_s=_STDIN_RECLAIM_BUDGET_S
                + (
                    ExecutionContext().cancel_grace + _POST_KILL_SETTLEMENT_S
                    if policy is ProcessGroupPolicy.OWN_GROUP
                    else 0
                ),
            )
            assert expired.timeout == _DEADLINE_S, (
                "the expiry must report the timeout the caller configured; a "
                "deadline being reported as the remainder it had left would "
                "make one field mean two things on two routes; got "
                f"{expired.timeout!r}"
            )
            descendant_pid = recorded_descendant_or_fail(pid_file)
            if policy is ProcessGroupPolicy.INHERIT:
                assert process_is_running(descendant_pid), (
                    "an inherited run must leave its descendant alive"
                )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())
