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
import io
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
from cuprum.capture_policy import BoundedCapture, FileBackedCapture
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
    "cancel_context_while_output_blocked",
    "cancel_waiter_while_stdin_blocked",
    "stalled_then_resumed",
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
def test_repeated_cancellation_during_teardown_still_settles() -> None:
    """Cancelling a waiter and the owning context still settles exactly once."""

    async def scenario() -> None:
        """Cancel a holder and a waiter together during teardown."""
        handle = await _PYTHON("-c", "import time; time.sleep(30)").start_bytes()

        async def hold() -> None:
            """Own the handle until cancellation tears the block down."""
            async with handle:
                await asyncio.sleep(30)

        holder = asyncio.create_task(hold())
        waiter = asyncio.create_task(handle.wait())
        await asyncio.sleep(0.1)
        holder.cancel()
        waiter.cancel()
        for task in (holder, waiter):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert handle.result is not None, "the settlement must still be recorded"
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        assert pending == [], f"owned tasks were left pending: {pending!r}"

    asyncio.run(scenario())


@pytest.mark.timeout(20)
def test_cancellation_with_blocked_stdin_finalizes_the_producer() -> None:
    """Cancelling a wait while the producer is blocked still finalizes it."""

    async def scenario() -> None:
        """Block a producer, cancel the waiter, then stop the child."""
        finalized = asyncio.Event()

        async def produce() -> cabc.AsyncIterator[bytes]:
            """Offer more than the pipe can take, so the writer blocks in drain."""
            try:
                await asyncio.sleep(0)
                yield b"x" * (4 * 1024 * 1024)
                yield b"y"
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


@pytest.mark.skipif(
    not _FD_DIRECTORY.is_dir(),
    reason="counting open descriptors requires procfs",
)
@pytest.mark.timeout(20)
def test_cancellation_with_blocked_output_reaps_the_child() -> None:
    """Cancelling the owning context while a live put is blocked still settles.

    Nothing reads the queue and no wait starts a settlement, so the drain is
    genuinely parked on a full queue when the cancellation arrives. Teardown
    itself has to release it.
    """

    async def scenario() -> None:
        """Fill a small queue, cancel the holder, and assert cleanup."""
        code = (
            "import sys, time\n"
            "sys.stdout.buffer.write(b'x' * 131072)\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(30)\n"
        )
        before = _open_fd_count()
        handle = await _PYTHON("-c", code).start_bytes(max_buffered_bytes=1024)

        async def hold() -> None:
            """Own the handle without consuming, until cancellation teardown."""
            async with handle:
                await asyncio.sleep(30)

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0.3)
        assert handle.result is None, "nothing must settle before the cancellation"
        assert not handle.stdout.at_eof, "the reader must still be live"
        holder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder
        assert handle.result is not None, "teardown must settle the handle"
        assert handle.result.exit_code in {-15, -9}, (
            f"the child ended as {handle.result.exit_code}"
        )
        assert handle.stdout.closure in {
            StreamClosure.EOF,
            StreamClosure.TRUNCATED_BY_SETTLEMENT,
        }, f"the reader ended as {handle.stdout.closure!r}"
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        assert pending == [], f"owned tasks were left pending: {pending!r}"
        assert _open_fd_count() == before, "the cancellation leaked a descriptor"

    asyncio.run(scenario())


@pytest.mark.timeout(20)
def test_bounded_capture_coexists_with_live_delivery() -> None:
    """A bounded capture shortens the result without shortening the live bytes."""

    async def scenario() -> None:
        """Deliver a payload live while its capture keeps only the tail."""
        code = "import sys; sys.stdout.buffer.write(b'x' * 4096)"
        options = RunOutputOptions(capture_stdout=BoundedCapture(max_bytes=1024))
        async with await _PYTHON("-c", code).start_bytes(output=options) as handle:
            received = await _read_all(handle.stdout)
            result = await handle.wait()
        assert received == b"x" * 4096, (
            f"the live reader must be lossless; got {len(received)} bytes"
        )
        captured = result.stdout
        assert captured is not None, "a bounded stdout capture must report bytes"
        assert captured == b"x" * 1024, (
            f"the bounded capture must keep the tail; got {len(captured)} bytes"
        )
        record = result.captures[0]
        assert record.total_bytes == 4096, (
            f"the record must count every byte; got {record.total_bytes}"
        )
        assert record.dropped_bytes == 3072, (
            f"the record must report the discarded bytes; got {record.dropped_bytes}"
        )
        assert record.truncated is True, "a bounded capture must report truncation"

    asyncio.run(scenario())


@pytest.mark.timeout(20)
def test_file_backed_capture_coexists_with_live_delivery(tmp_path: Path) -> None:
    """A spilled capture retains the whole payload beside the live bytes."""

    async def scenario() -> None:
        """Deliver a payload live while its capture spills to a file."""
        code = "import sys; sys.stdout.buffer.write(b'y' * 4096)"
        options = RunOutputOptions(
            capture_stdout=FileBackedCapture(
                spill_threshold_bytes=1,
                directory=tmp_path,
                keep_file=True,
            ),
        )
        async with await _PYTHON("-c", code).start_bytes(output=options) as handle:
            received = await _read_all(handle.stdout)
            result = await handle.wait()
        assert received == b"y" * 4096, (
            f"the live reader must be lossless; got {len(received)} bytes"
        )
        captured = result.stdout
        assert captured is not None, "a spilled capture must report bytes"
        assert captured == b"y" * 4096, (
            f"a spilled capture must still return the bytes; got {len(captured)}"
        )
        record = result.captures[0]
        assert record.storage == "file", f"the capture storage was {record.storage!r}"
        assert record.path is not None, "a spilled capture must name its file"
        spilled = await asyncio.to_thread(record.path.read_bytes)
        assert spilled == b"y" * 4096, (
            "the reported file must hold the stream the reader saw"
        )

    asyncio.run(scenario())


@pytest.mark.timeout(20)
def test_echo_coexists_with_live_delivery() -> None:
    """Echo writes the decoded stream while the reader receives its bytes."""

    async def scenario() -> None:
        """Deliver both streams live and echo them to the context's sinks."""
        stdout_sink = io.StringIO()
        stderr_sink = io.StringIO()
        code = "import sys; sys.stdout.write('out'); sys.stderr.write('err')"
        context = ExecutionContext(
            stdout_sink=stdout_sink,
            stderr_sink=stderr_sink,
        )
        handle = await _PYTHON("-c", code).start_bytes(
            output=RunOutputOptions(echo=True),
            context=context,
        )
        async with handle:
            received_out = await _read_all(handle.stdout)
            received_err = await _read_all(handle.stderr)
            result = await handle.wait()
        assert received_out == b"out", f"live stdout was {received_out!r}"
        assert received_err == b"err", f"live stderr was {received_err!r}"
        assert result.stdout == b"out", f"captured stdout was {result.stdout!r}"
        assert result.stderr == b"err", f"captured stderr was {result.stderr!r}"
        assert stdout_sink.getvalue() == "out", (
            f"the stdout echo wrote {stdout_sink.getvalue()!r}"
        )
        assert stderr_sink.getvalue() == "err", (
            f"the stderr echo wrote {stderr_sink.getvalue()!r}"
        )

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
