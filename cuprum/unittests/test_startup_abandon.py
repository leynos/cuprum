"""What the startup-abandonment helper owes a run that failed after its spawn.

A child spawned by ``run()``, ``run_sync()`` or ``start()`` belongs to the run
only once the setup that follows the spawn has returned. When that setup
raises, no route that would normally wait for, drain, and reap the child is
entered, so :func:`~cuprum._subprocess_startup_abandon._abandon_started_process`
performs the reclaim itself.

Three obligations are exercised here against a genuinely spawned child, because
none is observable from a stub: the child must be reaped, the capture spill
file it may already own must be removed, and the parent-side descriptors of its
output pipes must be closed before the event loop is torn down. The spill
matters because a drain that is cancelled rather than settled never builds the
result that would have carried the spill handle, so the accumulator is the last
reference to a temporary file the library created and is therefore responsible
for. The descriptors matter because a descendant that inherited an output pipe
can outlive the child and keep it from ever reaching end-of-file, and reaping
the direct child closes nothing of the parent's.
"""

from __future__ import annotations

import asyncio
import sys
import time
import typing as typ

from cuprum import ScopeConfig, scoped, sh
from cuprum._pipeline_stream_fds import _extract_stream_fd
from cuprum._pipeline_types import _ExecutionHooks, _StageObservation
from cuprum._stdio_plan import _resolve_stdio
from cuprum._subprocess_execution import _SubprocessExecution
from cuprum._subprocess_startup_abandon import _abandon_started_process
from cuprum._subprocess_streams import _prepare_stream_spawn
from cuprum.capture_policy import FileBackedCapture
from cuprum.sh import ExecutionContext, RunOutputOptions
from cuprum.unittests._startup_abandon_support import (
    _is_fd_open,
    _is_running,
    _kill_descendant,
    _recorded_pid,
    _spawn_output_pipe_holder,
    _wait_for_pid_file,
)
from cuprum.unittests._wait4_process_support import _wait4_only
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import pathlib

    from cuprum._capture_types import CaptureFinal


#: How long the descendant in the pipe-release case stays alive. Long enough
#: that a reclaim which waited for it to exit could not finish inside the
#: case's own bound, so passing means the bounded reclaim ended it, not that
#: the descendant happened to finish first.
_DESCENDANT_LIFETIME_S = 30

#: How long the case waits for the child to record its descendant's pid. The
#: child only starts an interpreter and forks, but this host is shared.
_PID_FILE_WAIT_S = 10.0

#: Upper bound on the reclaim in the pipe-release case. The discard is bounded
#: per stream by the capture EOF grace, so a healthy reclaim settles well
#: inside a second; anything approaching the descendant's lifetime means the
#: reclaim waited for an exit that never comes.
_RECLAIM_BOUND_S = 5.0


class _UnremovableSpillError(OSError):
    """Raised by the failing sink, standing in for an unremovable spill file."""


class _SinkMisuseError(AssertionError):
    """Raised when a case drives the failing sink past what it stands for."""


class _FailingSink:
    """A capture sink whose file cannot be removed, as a stuck spill would be."""

    async def absorb(self, chunk: bytes) -> None:
        """Take one raw chunk, which this sink has no use for."""

    async def finalize(self) -> CaptureFinal:
        """Fail: a case that finalizes this sink is testing the wrong object."""
        raise _SinkMisuseError

    async def cleanup(self) -> None:
        """Fail, standing in for a spill file that cannot be removed."""
        raise _UnremovableSpillError


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


def test_a_failing_capture_does_not_strand_the_other_stream(
    tmp_path: pathlib.Path,
) -> None:
    """One stream's failed cleanup must not leave the other stream's file behind.

    The reclaim releases both streams' accumulators, and a sink that cannot
    remove its file reports that by raising. Releasing the pair as one sequence
    would stop at the failure and leave the second stream's spill file to a run
    that never builds the result which would close it. The failing sink is
    given the first turn, so the removal of the real file behind it is what
    shows each accumulator is released on its own.
    """

    async def run_case() -> None:
        """Abandon a real child with a failing sink ahead of a real one."""
        process = await _spawn_sleeper()
        execution = _execution(
            FileBackedCapture(spill_threshold_bytes=None, directory=tmp_path),
        )
        sink = _prepare_stream_spawn(execution, process.pid).spawn_context.captures[0]
        assert sink is not None, "a captured stdout must have an accumulator"
        await sink.absorb(b"spilled past the threshold")

        await _abandon_started_process(
            process,
            execution,
            captures=(_FailingSink(), sink),
        )

        assert not _spill_files(tmp_path), (
            "a failure removing the first stream's file must not stop the "
            "second stream's file from being removed"
        )

    asyncio.run(run_case())


@_wait4_only
def test_abandon_releases_output_pipes_a_descendant_keeps_open(
    tmp_path: pathlib.Path,
) -> None:
    """An abandoned run must not leave its output pipes to the descendant.

    The child spawns a descendant that inherits stdout and stderr, then waits
    to be signalled. Terminating the child therefore leaves the write ends
    open, so neither pipe ever reaches end-of-file and the discard's grace
    expires with the read transports still holding the parent-side
    descriptors. Reaping the child does not release them: the ``wait4`` owner
    collects the status and nothing else, and no other step closes its
    transports. The reclaim must close them itself, and it must do so before
    this coroutine returns, because the event loop is torn down immediately
    afterwards.

    The descendant is asserted to still be running when the descriptors are
    read, so a case that passed because the descendant had exited cannot be
    mistaken for one that released the pipes.
    """

    async def run_case() -> None:
        """Abandon a real child whose descendant keeps the output pipes open."""
        pid_file = tmp_path / "descendant.pid"
        process = await _spawn_output_pipe_holder(
            pid_file,
            _DESCENDANT_LIFETIME_S,
        )
        try:
            descendant_pid = await _wait_for_pid_file(pid_file, _PID_FILE_WAIT_S)
            descriptors = tuple(
                _extract_stream_fd(stream)
                for stream in (process.stdout, process.stderr)
            )
            assert all(fd is not None for fd in descriptors), (
                "each captured stream must have a parent-side pipe whose "
                "descriptor this case can watch"
            )
            started_at = time.monotonic()

            await _abandon_started_process(
                process,
                _execution(
                    FileBackedCapture(
                        spill_threshold_bytes=None,
                        directory=tmp_path,
                    ),
                ),
            )

            elapsed = time.monotonic() - started_at
            assert process.returncode is not None, (
                "the direct child must be reaped even though a descendant outlives it"
            )
            assert elapsed < _RECLAIM_BOUND_S, (
                f"the reclaim took {elapsed:.3f}s; it must be bounded by the "
                "end-of-file grace rather than wait for the descendant"
            )
            assert _is_running(descendant_pid), (
                "the descendant must still hold the pipes when the "
                "descriptors are read, or this case proves nothing"
            )
            for fd in descriptors:
                assert fd is not None
                assert not _is_fd_open(fd), (
                    f"descriptor {fd} must be closed before the event loop "
                    "is torn down, even though no end-of-file arrived"
                )
        finally:
            _kill_descendant(_recorded_pid(pid_file))

    asyncio.run(run_case())
