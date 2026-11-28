"""POSIX process-group stories for owned live byte streams.

A child can leave a descendant holding the output pipe open after the leader
exits. With an owned group the stop reaches the descendant and the reader sees
a clean EOF; with an inherited group the descendant survives, the reader is
truncated at settlement, and the scenario kills the survivor itself. The PID
marker is established before the wait so a failed or slow settlement still
cleans up.

Only POSIX has process groups; the calling test marks these scenarios
accordingly.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from cuprum import ExecutionContext, ProcessGroupPolicy, StreamClosure
from tests.helpers.live_byte_support import (
    DESCENDANT_WAIT_S,
    HANDLE_DEADLINE_S,
    PYTHON,
    SETTLEMENT_DEADLINE_S,
    is_alive,
    kill_and_reap,
    require,
    wait_dead,
)

__all__ = ["descendant_inherit", "descendant_own_group"]


def descendant_child_code(marker: Path) -> str:
    """Build the leader's code: spawn a pipe-holding descendant, then exit."""
    descendant = "import time; time.sleep(30)"
    return (
        "import subprocess, sys\n"
        "child = subprocess.Popen(\n"
        f"    [sys.executable, '-c', {descendant!r}],\n"
        "    stdout=sys.stdout.fileno(),\n"
        "    stderr=sys.stderr.fileno(),\n"
        ")\n"
        f"with open({str(marker)!r}, 'w') as stream:\n"
        "    stream.write(str(child.pid))\n"
        "print('leader-done', flush=True)\n"
    )


async def descendant_scenario(*, own_group: bool) -> None:
    """Settle a run whose child leaves a descendant holding the output pipe."""
    directory = Path(tempfile.mkdtemp(prefix="cuprum-live-"))
    marker = directory / "descendant.pid"
    try:
        code = descendant_child_code(marker)
        policy = (
            ProcessGroupPolicy.OWN_GROUP if own_group else ProcessGroupPolicy.INHERIT
        )
        context = ExecutionContext(process_group=policy, cancel_grace=0.2)
        handle = await PYTHON("-c", code).start_bytes(context=context)
        if own_group:
            publish_owned_group(handle.pid)
        started = time.monotonic()
        result = await handle.wait(timeout=HANDLE_DEADLINE_S)
        elapsed = time.monotonic() - started
        require(elapsed < SETTLEMENT_DEADLINE_S, f"settlement took {elapsed:.2f}s")
        require(result.ok, f"the leader exited {result.exit_code}")
        require(marker.is_file(), "the descendant PID marker was not written")
        descendant = int(marker.read_text(encoding="utf-8"))
        if own_group:
            require(
                wait_dead(descendant, DESCENDANT_WAIT_S),
                "an owned group must reap the descendant holding the pipe",
            )
            require(
                handle.stdout.closure is StreamClosure.EOF,
                "the group stop should let stdout reach EOF, got "
                f"{handle.stdout.closure!r}",
            )
        else:
            # The survival assertion must run before the cleanup below.
            require(is_alive(descendant), "an inherited descendant must survive")
            require(
                handle.stdout.closure is StreamClosure.TRUNCATED_BY_SETTLEMENT,
                "an inherited descendant must truncate the reader, got "
                f"{handle.stdout.closure!r}",
            )
    finally:
        # Cleanup is established before the wait so a slow or raising
        # settlement cannot leave a 30-second sleeper or a scratch directory.
        kill_marker_descendant(marker)
        shutil.rmtree(directory, ignore_errors=True)


def publish_owned_group(pid: int | None) -> None:
    """Publish an owned group's identifier for a supervisor timeout reclaim.

    ``OWN_GROUP`` makes the child the leader of a new session, so the
    supervisor's own session kill cannot reach it. Writing the identifier once
    the child exists lets a rescued timeout still kill that group.
    """
    if pid is None:
        return
    directory = os.environ.get("CUPRUM_LIVE_SCENARIO_DIR")
    if directory:
        Path(directory, "owned-group.pid").write_text(str(pid), encoding="utf-8")


def kill_marker_descendant(marker: Path) -> None:
    """Kill and reap the descendant recorded in *marker*, if one was written."""
    if not marker.is_file():
        return
    text = marker.read_text(encoding="utf-8").strip()
    if text.isdigit():
        kill_and_reap(int(text))


async def descendant_own_group() -> None:
    """Reap a descendant that holds the pipe when the run owns its group."""
    await descendant_scenario(own_group=True)


async def descendant_inherit() -> None:
    """Leave a descendant alive when the run inherits the parent's group."""
    await descendant_scenario(own_group=False)
