"""Direct-run writer settlement without a configured command deadline.

These cases exercise the fixed settle window and cancellation cleanup. The
configured-deadline route matrix lives in test_run_no_stream_post_exit_deadline.
Both modules share only the existing process and task coordination fixtures.
"""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import pytest

from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions, StdinInput
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
    start_owned_run,
)
from tests.helpers.timeouts import (
    descendant_then_exit_argv,
    process_is_running,
    wait_for_pid_file,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import SafeCmd

__all__ = ["owned_builder"]
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process groups are unavailable on Windows"
)


def test_a_run_without_a_timeout_still_bounds_its_post_exit_writer_wait(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    watch = SpawnedWriter(monkeypatch)

    async def run_case() -> None:
        """Run without a timeout and assert it ends inside the harness budget."""
        descendant_pid: int | None = None
        try:
            result = await asyncio.wait_for(
                command.run(
                    stdin=StdinInput(data=b"x" * PARKING_STDIN_BYTES),
                    output=RunOutputOptions(capture=False, echo=False),
                    context=ExecutionContext(process_group=ProcessGroupPolicy.INHERIT),
                ),
                RUN_BUDGET_S,
            )
            descendant_pid = recorded_descendant_or_fail(pid_file)
            watch.assert_settled_after_run()
            assert process_is_running(descendant_pid), (
                "a no-timeout inherited run must leave its descendant alive"
            )
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
