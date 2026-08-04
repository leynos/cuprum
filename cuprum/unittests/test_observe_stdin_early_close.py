"""Deterministic early-close coordination test for stdin observe events.

Extracted from ``test_observe`` to keep that module within the repository's
file-size budget. This file owns the go/closed marker handshake that drives a
subprocess into a broken-pipe (EPIPE) failure and asserts the runner surfaces it
as an observable ``stdin_error`` event.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum import sh
from cuprum._subprocess_stdin import _write_stdin
from cuprum.context import ScopeConfig, scoped
from cuprum.sh import ExecutionContext, RunOutputOptions, StdinInput
from tests.helpers.catalogue import python_catalogue
from tests.helpers.stream_pipes import drain_blocking_payload_size

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from cuprum._pipeline_types import _StageObservation
    from cuprum.events import ExecEvent


# Child script for the early-close test. The child waits — with a finite
# deadline — for the parent's ``go`` marker (written only once the writer is
# confirmed wedged in drain), closes its stdin, then writes a ``closed`` marker
# to signal it has done so. If the marker never arrives (for example, the parent
# aborts before writing it) the child exits non-zero rather than spinning
# forever and orphaning the process.
_STDIN_EARLY_CLOSE_CHILD = "\n".join((
    "import os, sys, time",
    "from pathlib import Path",
    'go = Path(os.environ["CUPRUM_STDIN_CLOSE_GO"])',
    'closed = Path(os.environ["CUPRUM_STDIN_CLOSED"])',
    "deadline = time.monotonic() + 15.0",
    "while not go.exists():",
    "    if time.monotonic() >= deadline:",
    '        sys.exit("cuprum early-close child timed out waiting for go marker")',
    "    time.sleep(0.005)",
    "sys.stdin.close()",
    'closed.write_text("closed")',
    'print("done")',
))


async def _wait_for_path(path: Path, *, timeout: float) -> None:
    """Poll until ``path`` exists, raising ``TimeoutError`` on expiry."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not path.exists():
        if loop.time() >= deadline:
            msg = f"timed out waiting for marker {path}"
            raise TimeoutError(msg)
        await asyncio.sleep(0.005)


def _patch_writer_wedge_signal(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Patch the stdin writer to flag when it engages; return that flag."""
    writer_wedged = asyncio.Event()

    async def _coordinated_write_stdin(
        process: asyncio.subprocess.Process,
        stdin_data: bytes,
        observation: _StageObservation,
    ) -> None:
        """Signal the writer is running, then write stdin as usual."""
        writer_wedged.set()
        await _write_stdin(process, stdin_data, observation)

    monkeypatch.setattr(
        "cuprum._subprocess_stdin._write_stdin", _coordinated_write_stdin
    )
    return writer_wedged


def test_observe_emits_stdin_error_event_when_process_closes_stdin_early(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Emit a real ``stdin_error`` event when the subprocess closes stdin early.

    The runner writes a payload larger than the OS pipe buffer, so the real
    writer wedges in ``drain()``. Only once that writer is confirmed wedged is
    the child released to close its stdin, driving the wedged ``drain()`` into a
    broken-pipe (EPIPE) failure. The runner must surface that as an observable
    ``stdin_error`` event rather than propagating or swallowing it silently.

    A go/closed marker handshake replaces reliance on payload-size timing: the
    child closes stdin only after the writer is confirmed wedged, and signals
    back once it has, so the early-close path is coordinated deterministically.
    """
    catalogue, program = python_catalogue()
    builder = sh.make(program, catalogue=catalogue)
    writer_wedged = _patch_writer_wedge_signal(monkeypatch)
    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        """Record an emitted execution event."""
        events.append(ev)

    async def _orchestrate(go_path: Path, closed_path: Path) -> sh.CommandResult:
        """Run the command, releasing the child only once the writer wedges."""
        cmd = builder("-c", _STDIN_EARLY_CLOSE_CHILD)
        context = ExecutionContext(
            env={
                "CUPRUM_STDIN_CLOSE_GO": str(go_path),
                "CUPRUM_STDIN_CLOSED": str(closed_path),
            },
        )
        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            task = asyncio.create_task(
                cmd.run(
                    stdin=StdinInput(data=b"x" * drain_blocking_payload_size()),
                    output=RunOutputOptions(capture=True),
                    context=context,
                )
            )
            # Release the child only after the writer is confirmed wedged in
            # drain(): deterministic ordering rather than racing the close.
            await asyncio.wait_for(writer_wedged.wait(), timeout=5.0)
            go_path.write_text("go")
            # Confirm the child's post-close signal before finishing, so the
            # close is observed rather than merely inferred from timing.
            await _wait_for_path(closed_path, timeout=10.0)
            return await asyncio.wait_for(task, timeout=10.0)

    result = asyncio.run(_orchestrate(tmp_path / "go", tmp_path / "closed"))

    assert result.exit_code == 0, (
        f"subprocess exited with non-zero code: {result.exit_code}"
    )
    stdin_error_events = [ev for ev in events if ev.phase == "stdin_error"]
    assert stdin_error_events, (
        "expected at least one stdin_error event when subprocess closes stdin early"
    )
    assert stdin_error_events[0].pid is not None, (
        "expected pid to be present on first stdin_error event"
    )
    assert stdin_error_events[0].note is not None, (
        "expected error note/detail to be present on first stdin_error event"
    )
