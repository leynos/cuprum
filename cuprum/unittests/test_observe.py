"""Unit tests for structured execution events and observe hooks."""

from __future__ import annotations

import asyncio
import sys
import typing as typ
from pathlib import Path

from cuprum import sh
from cuprum._pipeline_internals import _EventDetails
from cuprum._subprocess_execution import _write_stdin
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.context import ScopeConfig, current_context, scoped
from cuprum.program import Program
from cuprum.sh import ExecutionContext, RunOutputOptions, StdinInput

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_internals import _StageObservation
    from cuprum.events import ExecEvent


class _FailingStdin:
    """Test double for stdin writer failures."""

    def __init__(
        self,
        *,
        drain_error: BaseException | None = None,
        wait_closed_error: BaseException | None = None,
    ) -> None:
        self.drain_error = drain_error
        self.wait_closed_error = wait_closed_error
        self.closed = False

    def write(self, data: bytes) -> None:
        """Accept bytes like ``asyncio.StreamWriter.write``."""
        _ = data

    async def drain(self) -> None:
        """Raise configured drain failure."""
        if self.drain_error is not None:
            raise self.drain_error

    def close(self) -> None:
        """Record close calls."""
        self.closed = True

    async def wait_closed(self) -> None:
        """Raise configured close-wait failure."""
        if self.wait_closed_error is not None:
            raise self.wait_closed_error


class _FakeProcess:
    """Test double exposing the subprocess fields used by ``_write_stdin``."""

    def __init__(self, stdin: _FailingStdin) -> None:
        self.stdin = stdin
        self.pid = 123


class _FakeObservation:
    """Collect emitted stdin error events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, _EventDetails]] = []

    def emit(self, phase: str, details: _EventDetails) -> None:
        """Record emitted event details."""
        self.events.append((phase, details))


def _python_builder(
    *, project_name: str = "observe-tests"
) -> tuple[cabc.Callable[..., sh.SafeCmd], ProgramCatalogue]:
    python_program = Program(str(Path(sys.executable)))
    project = ProjectSettings(
        name=project_name,
        programs=(python_program,),
        documentation_locations=("docs/users-guide.md",),
        noise_rules=(),
    )
    catalogue = ProgramCatalogue(projects=(project,))
    return sh.make(python_program, catalogue=catalogue), catalogue


def _run_with_observe(
    cmd: sh.SafeCmd | sh.Pipeline,
    *,
    allowlist: frozenset[Program],
    context: ExecutionContext | None = None,
) -> tuple[object, list[ExecEvent]]:
    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        events.append(ev)

    with scoped(ScopeConfig(allowlist=allowlist)), sh.observe(hook):
        result = cmd.run_sync(context=context)
    return result, events


def test_observe_registration_detaches_cleanly() -> None:
    """observe() registers and detaches from the current context."""
    builder, catalogue = _python_builder()
    cmd = builder("-c", "print('x')")
    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        events.append(ev)

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)):
        before_count = len(current_context().observe_hooks)
        registration = sh.observe(hook)
        with_hooks = current_context()
        assert len(with_hooks.observe_hooks) == before_count + 1

        _ = cmd.run_sync()
        assert events, "Expected observe hook to capture events while registered"

        registration.detach()
        restored = current_context()
        assert len(restored.observe_hooks) == before_count

        events.clear()
        _ = cmd.run_sync()
        assert not events, "Expected no observe events after detaching"


def test_observe_emits_stdout_stderr_timing_and_tags(tmp_path: Path) -> None:
    """Observe hooks receive line events, timing, and merged tags."""
    builder, catalogue = _python_builder(project_name="observe-runtime")
    cmd = builder(
        "-c",
        "\n".join(
            (
                "import sys",
                "print('out1')",
                "print('out2')",
                "print('err1', file=sys.stderr)",
            ),
        ),
    )
    result, events = _run_with_observe(
        cmd,
        allowlist=catalogue.allowlist,
        context=ExecutionContext(
            cwd=tmp_path,
            env={"CUPRUM_OBSERVE": "1"},
            tags={"run_id": "unit"},
        ),
    )

    assert typ.cast("sh.CommandResult", result).exit_code == 0

    assert events[0].phase == "plan"
    assert events[1].phase == "start"
    assert events[-1].phase == "exit"

    assert "out1" in {ev.line for ev in events if ev.phase == "stdout"}
    assert "out2" in {ev.line for ev in events if ev.phase == "stdout"}
    assert "err1" in {ev.line for ev in events if ev.phase == "stderr"}

    start_event = next(ev for ev in events if ev.phase == "start")
    exit_event = next(ev for ev in events if ev.phase == "exit")
    assert start_event.pid is not None
    assert start_event.pid > 0
    assert exit_event.exit_code == 0
    assert exit_event.duration_s is not None
    assert exit_event.duration_s >= 0.0

    assert start_event.cwd == tmp_path
    assert start_event.env is not None
    assert start_event.env["CUPRUM_OBSERVE"] == "1"

    assert start_event.tags["project"] == "observe-runtime"
    assert start_event.tags["run_id"] == "unit"


def test_pipeline_observe_emits_stage_tags_and_final_stdout() -> None:
    """Pipeline execution emits per-stage events with stage tags."""
    builder, catalogue = _python_builder(project_name="observe-pipeline")
    stage1 = builder("-c", "print('hello')")
    stage2 = builder(
        "-c",
        "\n".join(
            (
                "import sys",
                "data = sys.stdin.read()",
                "sys.stdout.write(data.upper())",
            ),
        ),
    )
    pipeline = stage1 | stage2

    result, events = _run_with_observe(pipeline, allowlist=catalogue.allowlist)

    assert typ.cast("sh.PipelineResult", result).ok is True
    assert typ.cast("sh.PipelineResult", result).stdout == "HELLO\n"

    exit_events = [ev for ev in events if ev.phase == "exit"]
    assert len(exit_events) == 2

    assert {typ.cast("int", ev.tags["pipeline_stage_index"]) for ev in exit_events} == {
        0,
        1,
    }
    assert "HELLO" in [
        ev.line
        for ev in events
        if ev.phase == "stdout"
        and typ.cast("int", ev.tags["pipeline_stage_index"]) == 1
    ]


def test_observe_emits_stdin_error_event_when_process_closes_stdin_early() -> None:
    """Emit ``stdin_error`` event when the subprocess closes stdin early."""
    builder, catalogue = _python_builder(project_name="observe-stdin-error")
    # This command reads a byte to unblock the writer, then closes stdin.
    cmd = builder(
        "-c",
        "import sys; sys.stdin.read(1); sys.stdin.close(); print('done')",
    )

    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        events.append(ev)

    large_payload = b"x" * 262144  # 256 KiB; exceeds typical pipe buffer
    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        result = cmd.run_sync(
            stdin=StdinInput(data=large_payload),
            output=RunOutputOptions(capture=True),
        )

    assert result.exit_code == 0
    stdin_error_events = [ev for ev in events if ev.phase == "stdin_error"]
    assert stdin_error_events, (
        "Expected at least one stdin_error event when the subprocess closes stdin early"
    )
    first = stdin_error_events[0]
    assert first.pid is not None
    assert first.note is not None  # error type/detail should be present


def test_write_stdin_observes_os_error_from_drain() -> None:
    """Emit stdin_error details for non-broken-pipe OSError failures."""
    stdin = _FailingStdin(drain_error=OSError("disk quota-ish"))
    process = _FakeProcess(stdin)
    observation = _FakeObservation()

    asyncio.run(
        _write_stdin(
            typ.cast("asyncio.subprocess.Process", process),
            b"payload",
            typ.cast("_StageObservation", observation),
        )
    )

    assert stdin.closed is True
    assert observation.events == [
        (
            "stdin_error",
            _EventDetails(pid=123, note="OSError: disk quota-ish"),
        )
    ]


def test_write_stdin_observes_runtime_error_from_wait_closed() -> None:
    """Emit stdin_error details when wait_closed fails."""
    stdin = _FailingStdin(wait_closed_error=RuntimeError("loop closed"))
    process = _FakeProcess(stdin)
    observation = _FakeObservation()

    asyncio.run(
        _write_stdin(
            typ.cast("asyncio.subprocess.Process", process),
            b"payload",
            typ.cast("_StageObservation", observation),
        )
    )

    assert stdin.closed is True
    assert observation.events == [
        (
            "stdin_error",
            _EventDetails(pid=123, note="RuntimeError: loop closed"),
        )
    ]
