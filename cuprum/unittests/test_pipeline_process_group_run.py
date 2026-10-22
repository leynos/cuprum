"""A public pipeline run under the opt-in process-group ownership policy.

The other pipeline-ownership tests drive the private spawn and cleanup helpers
with process doubles, which is what makes per-stage signal routing observable
in isolation. These tests drive ``Pipeline.run()`` itself instead: the whole
run, its own spawn, its own deadline, and its own teardown, over a first stage
that leaves a ``SIGTERM``-immune grandchild holding the run's inherited pipes.
That grandchild is the reason the option exists, and reaching it is the only
thing that lets the deadline be honoured.

Liveness is taken from the grandchild's recorded pid, so every signal these
tests send stays inside the group the run created rather than the test
runner's own.
"""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions
from cuprum.unittests._post_exit_teardown_support import reap_descendant
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import (
    descendant_then_exit_argv,
    pipe_holding_child_argv,
    process_is_running,
    wait_for_pid_file,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only

# Generous next to the milliseconds an informed teardown takes, and far short
# of the 120s the surviving stage sleeps for. The gap is the point: a run that
# is going to finish does so long before this, and a run that is not has
# nothing left to wait for.
_FAIL_FAST_RUN_BUDGET_S = 20.0


def _assert_descendant_reclaimed(descendant_pid: int, message: str) -> None:
    """Assert the run reclaimed the descendant that held the inherited pipes."""
    wait_for_process_death(
        descendant_pid,
        context="an owned failing stage's group teardown",
    )
    assert not process_is_running(descendant_pid), message


async def _run_owned_failure(pipeline: sh.Pipeline) -> sh.PipelineResult:
    """Run *pipeline* owned and undeadlined, bounded so a leak fails loudly.

    A descendant left holding the run's inherited pipes holds the run open
    rather than failing it, so without a bound this state would present as a
    hang. The bound converts it into a named defect instead: a run that
    outlasts it fails the test here, naming the cause rather than timing out
    somewhere upstream.

    Returns
    -------
    sh.PipelineResult
        The completed run's result, including its latched failure index.
    """
    try:
        return await asyncio.wait_for(
            pipeline.run(
                output=RunOutputOptions(capture=True, echo=False),
                context=ExecutionContext(
                    process_group=ProcessGroupPolicy.OWN_GROUP,
                ),
            ),
            _FAIL_FAST_RUN_BUDGET_S,
        )
    except TimeoutError:
        pytest.fail(
            "the run did not finish within "
            f"{_FAIL_FAST_RUN_BUDGET_S}s: an owned stage's descendant is still "
            "holding the run's inherited pipes, so nothing reached its group"
        )


def test_a_public_owned_pipeline_run_reaches_its_grandchildren(tmp_path: Path) -> None:
    """A pipeline run asked for ownership reclaims the grandchild it started.

    The public entry point, end to end: ``Pipeline.run()`` with
    ``ProcessGroupPolicy.OWN_GROUP`` stages. The first stage spawns a
    grandchild that inherits the run's pipes and ignores ``SIGTERM``, so the
    deadline can only be honoured if teardown signals the group the stage
    leads. The direct child is reaped by the run itself, which is exactly the
    case where a direct-child-only teardown leaves the grandchild running.
    """

    async def run_case() -> None:
        """Run an owned two-stage pipeline over a pipe-holding child."""
        pid_file = tmp_path / "owned-grandchild.pid"
        catalogue, python_program = python_catalogue()
        python = sh.make(python_program, catalogue=catalogue)
        pipeline = python(*pipe_holding_child_argv(pid_file)) | python("-c", "pass")

        grandchild_pid: int | None = None
        try:
            with (
                scoped(ScopeConfig(allowlist=frozenset({python_program}))),
                pytest.raises(sh.TimeoutExpired),
            ):
                await pipeline.run(
                    output=RunOutputOptions(capture=True, echo=False),
                    timeout=1.0,
                    context=ExecutionContext(
                        process_group=ProcessGroupPolicy.OWN_GROUP,
                    ),
                )
            grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
            _assert_descendant_reclaimed(
                grandchild_pid,
                "an owned pipeline run must reclaim the descendant its stage "
                "left behind, rather than leaving it holding the inherited pipes",
            )
        finally:
            reap_descendant(pid_file, grandchild_pid)

    asyncio.run(run_case())


def test_an_owned_failing_stage_reaches_the_descendant_it_left(tmp_path: Path) -> None:
    """A failing owned stage is torn down through its group, not its exit code.

    The stage that fails is the stage whose group most needs reaching: it is
    the one the run is reacting to, and the one most likely to have started
    something it did not get to finish. Selecting fail-fast targets by exit
    status alone skips it — it has already exited, so its wait task is done —
    and the descendant it left behind then survives the run that spawned it,
    still holding the inherited pipes.

    The failing stage here blocks on nothing and exits non-zero immediately
    after its descendant is announced, so the pipeline is still waiting on its
    sibling when the failure latches. That is the window the selection rule
    has to get right.

    The failing stage's group is settled by the fail-fast teardown itself, not
    by the pipeline's timeout path, so this run carries no deadline. A
    descendant left holding the pipes would then hold the run open rather than
    fail it, which is why the run is bounded here and the bound is reported as
    the defect it is.
    """
    pid_file = tmp_path / "failing-stage-grandchild.pid"

    async def run_case() -> None:
        """Run an owned pipeline whose first stage fails after forking."""
        catalogue, python_program = python_catalogue()
        python = sh.make(python_program, catalogue=catalogue)
        pipeline = python(*descendant_then_exit_argv(pid_file, exit_code=7)) | python(
            "-c",
            "import time; time.sleep(120)",
        )

        grandchild_pid: int | None = None
        try:
            with scoped(ScopeConfig(allowlist=frozenset({python_program}))):
                result = await _run_owned_failure(pipeline)
            assert result.failure_index == 0, (
                f"the first stage must be the reported failure; got {result!r}"
            )
            grandchild_pid = wait_for_pid_file(
                pid_file,
                context="the failing stage's descendant",
            )
            _assert_descendant_reclaimed(
                grandchild_pid,
                "the stage that failed owns a group that outlives it, so its "
                "descendant must be reclaimed even though the stage itself "
                "had already exited when the failure latched",
            )
        finally:
            reap_descendant(pid_file, grandchild_pid)

    asyncio.run(run_case())


def test_an_owned_exited_sibling_still_reaches_the_descendant_it_left(
    tmp_path: Path,
) -> None:
    """A successful owned stage's group is reached once a sibling fails.

    The complement of the test above, and the case that separates ownership
    from exit status completely. The first stage *succeeded*: its wait task
    reports completion with a zero exit code well before the failure latches,
    so every direct-child rule agrees it needs nothing — a finished stage has
    no direct child left to signal. Its group nevertheless still holds the
    descendant it spawned, and that descendant still holds the run's
    inherited pipes.

    Selecting fail-fast targets by exit status alone therefore skips it, and
    the run's own collection then waits on pipes whose only writer is a
    process nothing has signalled. Reaching it has to come from the per-stage
    ownership record rather than from the failure or the waiter.

    The failure sits in the middle of three stages on purpose: a failing
    *final* stage has nothing left to stop, so it does not trigger fail-fast
    at all, and the window this test needs would never open.
    """
    pid_file = tmp_path / "exited-sibling-grandchild.pid"

    async def run_case() -> None:
        """Run an owned pipeline whose first stage succeeds after forking."""
        catalogue, python_program = python_catalogue()
        python = sh.make(python_program, catalogue=catalogue)
        pipeline = (
            python(*descendant_then_exit_argv(pid_file, exit_code=0))
            | python("-c", "import sys, time; time.sleep(1.0); sys.exit(9)")
            | python("-c", "import time; time.sleep(120)")
        )

        grandchild_pid: int | None = None
        try:
            with scoped(ScopeConfig(allowlist=frozenset({python_program}))):
                result = await _run_owned_failure(pipeline)
            assert result.failure_index == 1, (
                "the middle stage is the one that fails; the first succeeds "
                f"while leaving its descendant behind; got {result!r}"
            )
            grandchild_pid = wait_for_pid_file(
                pid_file,
                context="the exited sibling's descendant",
            )
            _assert_descendant_reclaimed(
                grandchild_pid,
                "a stage that exited successfully still owns the group it "
                "led, so the descendant it left must be reclaimed even though "
                "its waiter had already completed when the sibling failed",
            )
        finally:
            reap_descendant(pid_file, grandchild_pid)

    asyncio.run(run_case())
