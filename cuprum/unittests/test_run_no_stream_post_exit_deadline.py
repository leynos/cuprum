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

from cuprum.sh import (
    ExecutionContext,
    ProcessGroupPolicy,
    RunOutputOptions,
    SafeCmd,
    StdinInput,
)
from cuprum.unittests._post_exit_teardown_support import (
    PARKING_STDIN_BYTES,
    RUN_BUDGET_S,
    DirectCleanupWatch,
    SpawnedWriter,
    assert_reclaimed,
    cancel_twice_across_direct_cleanup,
    owned_builder,
    reap_descendant,
    recorded_descendant_or_fail,
    run_to_its_own_deadline,
    start_owned_run,
)
from tests.helpers.timeouts import (
    descendant_then_exit_argv,
    pending_tasks,
    wait_for_pid_file,
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
            await run_to_its_own_deadline(invoke(command))
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
            )
            assert expired.timeout == _DEADLINE_S, (
                "the expiry must report the timeout the caller configured; a "
                "deadline being reported as the remainder it had left would "
                "make one field mean two things on two routes; got "
                f"{expired.timeout!r}"
            )
            descendant_pid = recorded_descendant_or_fail(pid_file)
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_run_without_a_timeout_still_bounds_its_post_exit_writer_wait(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> None:
    """A run with no configured deadline is bounded by the settle window.

    The deadline cases above bound the writer with what is left of the run's
    own budget. A caller that configured no ``timeout=`` has no remainder to
    wait out, and an await bounded by ``None`` is not bounded at all — so the
    same parked writer that used to hang the deadlined run would hang this one
    for ever, with no expiry to report and no cleanup to reach it.

    The bound here is therefore the shared settle window rather than a
    deadline, and the assertion is that the run *returns* at all. It is run
    with the ``INHERIT`` policy so that no group signal can stand in for the
    settlement: under ``OWN_GROUP`` a group teardown would release the writer
    whether or not the settle reached it, and the case would pass on a run that
    never bounded the await.
    """
    pid_file = tmp_path / "direct-no-timeout.pid"
    command = owned_builder(*descendant_then_exit_argv(pid_file, exit_code=0))

    async def run_case() -> None:
        """Run without a timeout and assert it ends inside the harness budget."""
        descendant_pid: int | None = None
        try:
            result = await asyncio.wait_for(
                command.run(
                    stdin=StdinInput(data=b"x" * PARKING_STDIN_BYTES),
                    output=_DIRECT_OUTPUT,
                    context=ExecutionContext(process_group=ProcessGroupPolicy.INHERIT),
                ),
                RUN_BUDGET_S,
            )
            descendant_pid = recorded_descendant_or_fail(pid_file)
            assert result.exit_code == 0, (
                "the child exits successfully before the writer parks, so the "
                f"run must report its exit; got {result.exit_code!r}"
            )
        except TimeoutError:
            pytest.fail(
                "a direct run with no configured timeout must still bound its "
                "post-exit writer wait; without a remainder to spend, the "
                "settle window is the only bound there is"
            )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_second_cancel_during_the_direct_cleanup_still_settles_the_group(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct cleanup is one shielded unit, not two in sequence.

    The direct run's cleanup reclaims the stdin writer and then settles the
    group that writer was writing into. The second half is only reachable once
    the first has finished, and ``_shielded_cleanup`` finishes the task it was
    given before re-raising the caller's cancellation — so shielding the halves
    separately lets a *second* cancellation end the cleanup before the
    settlement ever starts. The descendant the child left behind is then never
    signalled, and it outlives the run that spawned it.

    The second cancellation models a caller insisting, or a deadline firing on
    top of a cancellation already in flight. Both halves of the evidence are
    asserted, because either alone is satisfiable by a broken run: the
    settlement being *entered* says the cleanup got that far, and the
    descendant being *gone* says it was not abandoned part-way through.

    Unlike the deadline cases above, this run configures no timeout. The
    absence is the point: nothing but the caller's cancellation ends it, so the
    cancellation is the only thing that can reach the cleanup being tested.
    """
    pid_file = tmp_path / "direct-cleanup-second-cancel.pid"
    watch = DirectCleanupWatch(monkeypatch)

    async def run_case() -> None:
        """Cancel twice: once to enter cleanup, once during its first half."""
        # The descendant is read directly rather than through the fixture's
        # bookkeeping: the child exits *successfully* here, so this run never
        # reaches a deadline and ``recorded_descendant_or_fail`` would report
        # the wrong defect if the pid file were ever missing.
        descendant_pid: int | None = None
        run_task = start_owned_run(
            owned_builder,
            pid_file,
            stdin=StdinInput(data=b"x" * PARKING_STDIN_BYTES),
            capture=False,
        )
        try:
            descendant_pid = await asyncio.to_thread(
                wait_for_pid_file,
                pid_file,
                context="the descendant a twice-cancelled direct run must reach",
            )
            # The child exits once the descendant has announced itself, which
            # parks the writer on the payload nothing will read. Cancelling
            # there can only enter the post-exit cleanup, and the second
            # cancellation lands on the writer's reclamation.
            await cancel_twice_across_direct_cleanup(run_task, watch)
            assert watch.settled.is_set(), (
                "a second cancellation arriving during the writer's reclamation "
                "must not cost the run its group settlement: the two halves "
                "have to be one shielded unit, or the helper re-raises out of "
                "the first and the descendant is never signalled"
            )
            await assert_reclaimed(
                descendant_pid,
                "the second cancellation ended the direct run's cleanup before "
                "the group was settled, so the descendant it left holding the "
                "run's stdin outlived it",
            )
        finally:
            watch.writer_gate.released.set()
            watch.cleanup_gate.released.set()
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())
