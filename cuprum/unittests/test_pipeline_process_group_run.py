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
    pipe_holding_child_argv,
    process_is_running,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only


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
