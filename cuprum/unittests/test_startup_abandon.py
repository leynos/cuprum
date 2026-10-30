"""What the startup-abandonment helper owes a run that failed after its spawn.

A child spawned by ``run()``, ``run_sync()`` or ``start()`` belongs to the run
only once the setup that follows the spawn has returned. When that setup
raises, no route that would normally wait for, drain, and reap the child is
entered, so :func:`~cuprum._subprocess_startup_abandon._abandon_started_process`
performs the reclaim itself.

Two obligations are exercised here against a genuinely spawned child, because
neither is observable from a stub: the child must be reaped, and the capture
spill file it may already own must be removed. The spill matters because a
drain that is cancelled rather than settled never builds the result that would
have carried the spill handle, so the accumulator is the last reference to a
temporary file the library created and is therefore responsible for.
"""

from __future__ import annotations

import asyncio
import sys
import time
import typing as typ

from cuprum import ScopeConfig, scoped, sh
from cuprum._pipeline_types import _ExecutionHooks, _StageObservation
from cuprum._stdio_plan import _resolve_stdio
from cuprum._subprocess_execution import _SubprocessExecution
from cuprum._subprocess_startup_abandon import _abandon_started_process
from cuprum._subprocess_streams import _prepare_stream_spawn
from cuprum.capture_policy import FileBackedCapture
from cuprum.sh import ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import pathlib


def _execution(policy: FileBackedCapture) -> _SubprocessExecution:
    """Build the run bundle the reclaim reads its teardown decisions from.

    Returns
    -------
    _SubprocessExecution
        A bundle that captures stdout under *policy* and leaves stderr
        uncaptured, which is enough for the stream prep to build the
        accumulator whose cleanup is under test.
    """
    catalogue, program = python_catalogue()
    with scoped(ScopeConfig(allowlist=catalogue.allowlist)):
        command = sh.make(program, catalogue=catalogue)("-c", "pass")
    return _SubprocessExecution(
        cmd=command,
        ctx=ExecutionContext(),
        capture_stdout=policy,
        capture_stderr=None,
        echo_stdout=False,
        echo_stderr=False,
        max_echo_line_bytes=None,
        sink_session=None,
        timeout=None,
        observation=_StageObservation(
            cmd=command,
            hooks=_ExecutionHooks(
                before_hooks=(),
                after_hooks=(),
                observe_hooks=(),
            ),
            tags={},
            cwd=None,
            env_overlay=None,
            pending_tasks=[],
            wall_clock=time.monotonic,
        ),
        stdio=_resolve_stdio(None, RunOutputOptions(capture=True)),
    )


async def _spawn_sleeper() -> asyncio.subprocess.Process:
    """Start a child that runs until it is signalled.

    Returns
    -------
    asyncio.subprocess.Process
        The running child, with all three standard streams piped.
    """
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


def _spill_files(directory: pathlib.Path) -> list[pathlib.Path]:
    """Return whatever the capture has left in its spill directory.

    Kept outside the case's coroutine so the listing is a plain call rather
    than a blocking filesystem method reached from an async function.

    Returns
    -------
    list[pathlib.Path]
        Every entry currently in *directory*; empty once the reclaim has run.
    """
    return list(directory.iterdir())


def test_abandon_reaps_the_child_and_removes_its_spill(tmp_path: pathlib.Path) -> None:
    """A failed startup reclaims the child and any spill it had already made.

    The accumulator is the real one the stream prep builds, and the chunk is
    absorbed by the test rather than by a consumer: this helper is reached
    before any consumer has run, so a spill can only exist here if the setup
    resolved its streams and something else filled them. The file is what the
    reclaim must remove, and the reaped child is what proves the sequence ran
    end to end rather than stopping at the capture step.
    """

    async def run_case() -> None:
        """Abandon a real child whose capture had already spilled."""
        process = await _spawn_sleeper()
        execution = _execution(
            FileBackedCapture(spill_threshold_bytes=None, directory=tmp_path),
        )
        sinks = _prepare_stream_spawn(execution, process.pid).spawn_context.captures
        sink = sinks[0]
        assert sink is not None, "a captured stdout must have an accumulator"
        await sink.absorb(b"spilled past the threshold")
        assert _spill_files(tmp_path), (
            "absorbing past a null threshold must create the spill file"
        )

        await _abandon_started_process(process, execution, captures=sinks)

        assert process.returncode is not None, (
            "the abandoned child must be reaped, not left running"
        )
        assert not _spill_files(tmp_path), (
            "the spill file must be removed, because no drain result will "
            "carry the handle that would otherwise clean it up"
        )

    asyncio.run(run_case())
