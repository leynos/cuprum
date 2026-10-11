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
import os
import signal
import sys
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions
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


def _recorded_descendant(pid_file: Path) -> int | None:
    """Return the descendant's recorded pid, or ``None`` if it never wrote one."""
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


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
            assert not process_is_running(grandchild_pid), (
                "an owned pipeline run must reclaim the descendant its stage "
                "left behind, rather than leaving it holding the inherited pipes"
            )
        finally:
            # A failure here means the descendant outlived its group teardown,
            # which is the very thing under test. Reap it before reporting, so
            # an assertion failure cannot leave a live process behind.
            if grandchild_pid is not None and process_is_running(grandchild_pid):
                os.kill(grandchild_pid, signal.SIGKILL)

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
                try:
                    result = await asyncio.wait_for(
                        pipeline.run(
                            output=RunOutputOptions(capture=True, echo=False),
                            context=ExecutionContext(
                                process_group=ProcessGroupPolicy.OWN_GROUP,
                            ),
                        ),
                        _FAIL_FAST_RUN_BUDGET_S,
                    )
                except TimeoutError:
                    grandchild_pid = _recorded_descendant(pid_file)
                    pytest.fail(
                        "the run did not finish within "
                        f"{_FAIL_FAST_RUN_BUDGET_S}s: the failed stage's "
                        "descendant is still holding the run's inherited "
                        "pipes, so nothing reached its group"
                    )
            assert result.failure_index == 0, (
                f"the first stage must be the reported failure; got {result!r}"
            )
            grandchild_pid = wait_for_pid_file(
                pid_file,
                context="the failing stage's descendant",
            )
            wait_for_process_death(
                grandchild_pid,
                context="an owned failing stage's group teardown",
            )
            assert not process_is_running(grandchild_pid), (
                "the stage that failed owns a group that outlives it, so its "
                "descendant must be reclaimed even though the stage itself "
                "had already exited when the failure latched"
            )
        finally:
            if grandchild_pid is None:
                grandchild_pid = _recorded_descendant(pid_file)
            if grandchild_pid is not None and process_is_running(grandchild_pid):
                os.kill(grandchild_pid, signal.SIGKILL)

    asyncio.run(run_case())
