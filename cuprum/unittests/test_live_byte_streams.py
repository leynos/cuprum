"""Real-child, externally supervised tests for owned live byte streams.

Each scenario runs in a separate interpreter under
:func:`tests.helpers.supervised_scenario.run_scenario`, which owns the
scenario's session and kills it on a hard deadline. A rescued timeout is
reported as a failure, so a hang can never be mistaken for a pass.

The assertions here are deliberately about observable bytes and closures:
reconstructed per-stream bytes, the captured result, the reader's terminal
state, and the child's recorded outcome. No assertion depends on read sizes or
on timing beyond the supervisor's deadline.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - starts a real session leader for the reclaim test.
import sys
import typing as typ
from pathlib import Path

import pytest

from cuprum import (
    ByteStreamReader,
    ExecutionContext,
    ForbiddenProgramError,
    Program,
    ProgramCatalogue,
    RunOutputOptions,
    ScopeConfig,
    StdinInput,
    StdinStream,
    StdioTarget,
    StreamClosure,
    scoped,
    sh,
)
from tests.helpers.supervised_scenario import (
    ScenarioResult,
    _reclaim_owned_group,
    run_scenario,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import BytesCommandResult, SafeCmdBuilder

_PORTABLE_SCENARIOS = (
    "prefix_before_input",
    "independent_eof",
    "malformed_split_utf8",
    "large_simultaneous",
    "stalled_then_wait",
    "cancel_while_blocked",
    "close_one_stream",
)

_POSIX_SCENARIOS = (
    "descendant_own_group",
    "descendant_inherit",
    # This scenario observes the child's raw PID to prove it was reaped, and
    # the standard library offers no non-signalling liveness probe on Windows.
    "live_failure_stops_held_child",
    "stdin_failure_stops_held_child",
)

_CATALOGUE = ProgramCatalogue.from_programs(sys.executable, name="live-bytes")
_PYTHON = sh.make(Program(sys.executable), catalogue=_CATALOGUE)


def _assert_ok(result: ScenarioResult) -> None:
    """Fail with the scenario's captured diagnostics unless it succeeded."""
    assert result.ok, (
        f"scenario {result.name} failed "
        f"(timed_out={result.timed_out}, returncode={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.mark.timeout(25)
@pytest.mark.parametrize("name", _PORTABLE_SCENARIOS)
def test_real_child_scenario_succeeds(name: str) -> None:
    """Every portable live-byte scenario completes within the watchdog."""
    _assert_ok(run_scenario(name))


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
@pytest.mark.timeout(25)
@pytest.mark.parametrize("wait_route", ["wait4", "fallback"])
@pytest.mark.parametrize("name", _POSIX_SCENARIOS)
def test_posix_scenario_succeeds(name: str, wait_route: str) -> None:
    """The POSIX-only scenarios settle on both reaping routes."""
    _assert_ok(
        run_scenario(
            name,
            env={"CUPRUM_LIVE_SCENARIO_WAIT_ROUTE": wait_route},
        )
    )


def test_start_bytes_rejects_a_line_observer_before_spawning() -> None:
    """A byte run cannot carry a caller-supplied line observer."""
    options = RunOutputOptions(on_line=lambda line: None)
    with pytest.raises(ValueError, match="on_line"):
        asyncio.run(_PYTHON("-c", "pass").start_bytes(output=options))


def test_start_bytes_rejects_a_redirected_stream() -> None:
    """A handle reads both streams through parent-side pipes."""
    options = RunOutputOptions(capture=False, stdout=StdioTarget.inherit())
    with pytest.raises(ValueError, match="stdout cannot be redirected"):
        asyncio.run(_PYTHON("-c", "pass").start_bytes(output=options))


def test_start_bytes_rejects_an_invalid_queue_bound() -> None:
    """The live queue bound is a strictly positive integer."""
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(_PYTHON("-c", "pass").start_bytes(max_buffered_bytes=0))
    with pytest.raises(TypeError, match="integer"):
        asyncio.run(_PYTHON("-c", "pass").start_bytes(max_buffered_bytes=True))


def test_start_bytes_enforces_the_allowlist() -> None:
    """A program outside the active scope is refused before a child exists."""
    other = Program("definitely-not-catalogued")
    catalogue = ProgramCatalogue.from_programs(
        Program(sys.executable),
        other,
        name="live-allowed",
    )
    allowed = sh.make(Program(sys.executable), catalogue=catalogue)
    forbidden = sh.make(other, catalogue=catalogue)
    with scoped(ScopeConfig(allowlist=frozenset({Program(sys.executable)}))):
        with pytest.raises(ForbiddenProgramError):
            asyncio.run(forbidden("-c", "pass").start_bytes())
        result = asyncio.run(_start_and_wait(allowed))
    assert result.ok, "the allowed program must still run"


async def _start_and_wait(command: SafeCmdBuilder) -> BytesCommandResult:
    """Start *command* with an empty byte run and return its result."""
    handle = await command("-c", "pass").start_bytes()
    return await handle.wait()


def test_start_bytes_accepts_a_complete_stdin_payload() -> None:
    """``StdinInput`` is accepted and encoded by the execution context."""

    async def scenario() -> None:
        """Write a text payload and reconstruct the echoed bytes."""
        code = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
        async with await _PYTHON("-c", code).start_bytes(
            stdin=StdinInput(text="héllo"),
            context=ExecutionContext(encoding="utf-8"),
        ) as handle:
            received = bytearray()
            async for chunk in handle.stdout:
                received.extend(chunk.data)
            result = await handle.wait()
        assert bytes(received) == "héllo".encode(), (
            f"stdin and stdout must round-trip, got {bytes(received)!r}"
        )
        assert result.stdout == bytes(received), "capture must equal the live bytes"

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
@pytest.mark.timeout(15)
def test_supervisor_reclaims_a_recorded_owned_group(tmp_path: Path) -> None:
    """A rescued timeout kills the group the scenario recorded."""
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    ) as process:
        (tmp_path / "owned-group.pid").write_text(str(process.pid), encoding="utf-8")
        try:
            _reclaim_owned_group(tmp_path)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        assert process.returncode == -signal.SIGKILL, (
            f"the recorded group must be killed, got {process.returncode}"
        )


def test_start_bytes_honours_a_capture_opt_out() -> None:
    """Live bytes still flow when the caller disables capture."""

    async def scenario() -> None:
        """Run with capture disabled and consume the live output."""
        code = "import sys; sys.stdout.buffer.write(b'live'); sys.stdout.buffer.flush()"
        options = RunOutputOptions(capture=False)
        async with await _PYTHON("-c", code).start_bytes(output=options) as handle:
            received = bytearray()
            async for chunk in handle.stdout:
                received.extend(chunk.data)
            result = await handle.wait()
        assert bytes(received) == b"live", (
            f"live delivery must be unaffected by capture, got {bytes(received)!r}"
        )
        assert result.stdout is None, "an opted-out stdout must not be captured"
        assert result.stderr is None, "an opted-out stderr must not be captured"

    asyncio.run(scenario())


def test_start_bytes_honours_a_per_stream_capture_opt_out() -> None:
    """One stream can be uncaptured while the other is retained."""

    async def scenario() -> None:
        """Disable stdout capture only."""
        code = (
            "import sys\n"
            "sys.stdout.buffer.write(b'out')\n"
            "sys.stdout.buffer.flush()\n"
            "sys.stderr.buffer.write(b'err')\n"
            "sys.stderr.buffer.flush()\n"
        )
        options = RunOutputOptions(capture_stdout=False)
        async with await _PYTHON("-c", code).start_bytes(output=options) as handle:
            out, err = await asyncio.gather(
                _read_all(handle.stdout),
                _read_all(handle.stderr),
            )
            result = await handle.wait()
        assert out == b"out", f"stdout live bytes were {out!r}"
        assert err == b"err", f"stderr live bytes were {err!r}"
        assert result.stdout is None, "the opted-out stdout must not be captured"
        assert result.stderr == b"err", "the captured stderr must be retained"

    asyncio.run(scenario())


async def _read_all(reader: ByteStreamReader) -> bytes:
    """Consume one reader to its end."""
    received = bytearray()
    async for chunk in reader:
        received.extend(chunk.data)
    return bytes(received)


_FD_DIRECTORY = Path("/proc/self/fd")


def _open_fd_count() -> int:
    """Count this process's open descriptors."""
    return sum(1 for _ in _FD_DIRECTORY.iterdir())


@pytest.mark.timeout(20)
def test_a_cancelled_waiter_leaves_the_settlement_running() -> None:
    """Cancelling one waiter does not cancel the handle's settlement."""

    async def scenario() -> None:
        """Cancel a waiter, then stop the child through the handle."""
        handle = await _PYTHON("-c", "import time; time.sleep(30)").start_bytes()
        waiter = asyncio.create_task(handle.wait())
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        result = await handle.kill()
        assert result.exit_code == -9, f"the child ended as {result.exit_code}"
        assert handle.result is not None, "the settlement must still be recorded"
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        assert pending == [], f"owned tasks were left pending: {pending!r}"

    asyncio.run(scenario())


@pytest.mark.timeout(20)
def test_repeated_cancellation_still_settles_the_handle() -> None:
    """Two cancelled waiters share one settlement that still completes."""

    async def scenario() -> None:
        """Cancel two waiters, then stop and settle the child."""
        handle = await _PYTHON("-c", "import time; time.sleep(30)").start_bytes()
        waiters = [asyncio.create_task(handle.wait()) for _ in range(2)]
        await asyncio.sleep(0.05)
        for waiter in waiters:
            waiter.cancel()
        for waiter in waiters:
            with pytest.raises(asyncio.CancelledError):
                await waiter
        result = await handle.kill()
        assert result.exit_code == -9, f"the child ended as {result.exit_code}"
        assert handle.result is not None, "the settlement must still be recorded"

    asyncio.run(scenario())


@pytest.mark.timeout(20)
def test_cancellation_with_blocked_stdin_finalizes_the_producer() -> None:
    """Cancelling a wait while the producer is blocked still finalizes it."""

    async def scenario() -> None:
        """Block a producer, cancel the waiter, then stop the child."""
        finalized = asyncio.Event()

        async def produce() -> cabc.AsyncIterator[bytes]:
            """Block until teardown finalizes this generator."""
            try:
                await asyncio.sleep(30)
                yield b"never"
            finally:
                finalized.set()

        handle = await _PYTHON("-c", "import time; time.sleep(30)").start_bytes(
            stdin=StdinStream(produce()),
            context=ExecutionContext(cancel_grace=0.2),
        )
        waiter = asyncio.create_task(handle.wait())
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        result = await handle.terminate(grace=0.2)
        await asyncio.wait_for(finalized.wait(), timeout=5.0)
        assert result.exit_code in {-15, -9}, f"the child ended as {result.exit_code}"

    asyncio.run(scenario())


@pytest.mark.timeout(20)
def test_cancellation_with_blocked_output_reaps_the_child() -> None:
    """Cancelling a wait while output is blocked still stops and reaps."""

    async def scenario() -> None:
        """Fill a small queue, cancel the waiter, then stop the child."""
        code = (
            "import sys, time\n"
            "sys.stdout.buffer.write(b'x' * 131072)\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(30)\n"
        )
        handle = await _PYTHON("-c", code).start_bytes(max_buffered_bytes=1024)
        waiter = asyncio.create_task(handle.wait())
        await asyncio.sleep(0.2)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        result = await handle.terminate(grace=0.2)
        assert result.exit_code in {-15, -9}, f"the child ended as {result.exit_code}"
        assert handle.stdout.closure in {
            StreamClosure.EOF,
            StreamClosure.TRUNCATED_BY_SETTLEMENT,
        }, f"the reader ended as {handle.stdout.closure!r}"

    asyncio.run(scenario())


@pytest.mark.skipif(
    not _FD_DIRECTORY.is_dir(),
    reason="counting open descriptors requires procfs",
)
@pytest.mark.timeout(20)
def test_a_live_run_releases_its_descriptors() -> None:
    """A completed exchange leaves no descriptor behind."""

    async def scenario() -> None:
        """Run one exchange to settlement."""
        code = "import sys; sys.stdout.buffer.write(b'live')"
        async with await _PYTHON("-c", code).start_bytes() as handle:
            received = await _read_all(handle.stdout)
            result = await handle.wait()
        assert received == b"live", f"live bytes were {received!r}"
        assert result.ok, f"the child ended as {result.exit_code}"

    before = _open_fd_count()
    asyncio.run(scenario())
    after = _open_fd_count()
    assert after == before, f"descriptor count changed from {before} to {after}"


@pytest.mark.parametrize("stream_backend", ["python", "rust"], indirect=True)
@pytest.mark.timeout(20)
def test_live_fidelity_is_independent_of_the_stream_backend(
    stream_backend: str,
) -> None:
    """The live byte path delivers the same bytes under either backend."""

    async def scenario() -> bytes:
        """Run a short exchange and return its live bytes."""
        code = "import sys; sys.stdout.buffer.write(b'backend')"
        async with await _PYTHON("-c", code).start_bytes() as handle:
            received = await _read_all(handle.stdout)
            result = await handle.wait()
        assert result.stdout == received, "capture must equal the live bytes"
        assert handle.stdout.at_eof, "the stream must reach EOF"
        return received

    assert asyncio.run(scenario()) == b"backend", (
        f"the {stream_backend} backend changed the live bytes"
    )
