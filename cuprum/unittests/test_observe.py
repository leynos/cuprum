"""Unit tests for structured execution events and observe hooks."""

from __future__ import annotations

import asyncio
import logging
import sys
import typing as typ
from pathlib import Path

import pytest

from cuprum import _subprocess_stdin, sh
from cuprum._pipeline_internals import _EventDetails
from cuprum._subprocess_stdin import _write_stdin
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
        """Initialize the fake with optional drain and close-wait failures."""
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
        """Attach fake stdin and pid attributes expected by ``_write_stdin``."""
        self.stdin = stdin
        self.pid = 123


class _FakeObservation:
    """Collect emitted stdin error events."""

    def __init__(self) -> None:
        """Initialize the collected event list."""
        self.events: list[tuple[str, _EventDetails]] = []

    def emit(self, phase: str, details: _EventDetails) -> None:
        """Record emitted event details."""
        self.events.append((phase, details))


def _python_builder(
    *, project_name: str = "observe-tests"
) -> tuple[cabc.Callable[..., sh.SafeCmd], ProgramCatalogue]:
    """Create a Python command builder and matching allowlist catalogue."""
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
    """Run a command or pipeline while collecting observe events."""
    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        """Record an emitted execution event."""
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
        """Record an emitted execution event."""
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


@pytest.mark.parametrize("execution_strategy", ["async", "sync"])
@pytest.mark.parametrize(
    "output",
    [
        pytest.param(RunOutputOptions(capture=False, echo=False), id="discard"),
        pytest.param(RunOutputOptions(capture=False, echo=True), id="echo"),
        pytest.param(RunOutputOptions(capture=True, echo=False), id="capture"),
        pytest.param(RunOutputOptions(capture=True, echo=True), id="tee"),
    ],
)
def test_observe_tags_reflect_run_output_options(
    output: RunOutputOptions,
    execution_strategy: typ.Literal["async", "sync"],
) -> None:
    """Observation tags expose the selected ``RunOutputOptions`` values."""
    builder, catalogue = _python_builder(project_name="observe-output-options")
    cmd = builder("-c", "pass")
    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        """Record an emitted execution event."""
        events.append(ev)

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        if execution_strategy == "async":
            result = asyncio.run(cmd.run(output=output))
        else:
            result = cmd.run_sync(output=output)

    assert result.exit_code == 0
    tagged_events = [ev for ev in events if ev.phase in {"plan", "start", "exit"}]
    assert [ev.phase for ev in tagged_events] == ["plan", "start", "exit"]
    for event in tagged_events:
        assert event.tags["capture"] is output.capture
        assert event.tags["echo"] is output.echo


def test_pipeline_awaits_scheduled_observe_tasks_before_return() -> None:
    """Pipeline execution awaits async observe hooks before returning."""
    builder, catalogue = _python_builder(project_name="observe-async-pipeline")
    stage1 = builder("-c", "print('hello')")
    stage2 = builder(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read().upper())",
    )
    completed_exit_stages: list[int] = []

    async def hook(event: ExecEvent) -> None:
        """Record exit events after an async scheduling boundary."""
        if event.phase != "exit":
            return
        await asyncio.sleep(0)
        completed_exit_stages.append(
            typ.cast("int", event.tags["pipeline_stage_index"])
        )

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        result = (stage1 | stage2).run_sync()

    assert result.ok is True, "pipeline should complete successfully"
    assert sorted(completed_exit_stages) == [0, 1], (
        "pipeline should await scheduled async exit observe tasks before returning"
    )


def test_pipeline_observe_emits_stage_tags_and_env_overlay() -> None:
    """Pipeline observation events retain tags and env overlays per stage."""
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

    result, events = _run_with_observe(
        pipeline,
        allowlist=catalogue.allowlist,
        context=ExecutionContext(
            env={"CUPRUM_OBSERVE_PIPELINE": "1"},
            tags={"run_id": "pipeline-unit"},
        ),
    )

    assert typ.cast("sh.PipelineResult", result).ok is True
    assert typ.cast("sh.PipelineResult", result).stdout == "HELLO\n"

    exit_events = [ev for ev in events if ev.phase == "exit"]
    assert len(exit_events) == 2

    assert {typ.cast("int", ev.tags["pipeline_stage_index"]) for ev in exit_events} == {
        0,
        1,
    }
    for event in exit_events:
        assert event.env is not None, "exit event should retain the environment"
        assert event.env["CUPRUM_OBSERVE_PIPELINE"] == "1", (
            "exit event should retain the pipeline environment value"
        )
        assert event.tags["project"] == "observe-pipeline", (
            "exit event should retain the project tag"
        )
        assert event.tags["run_id"] == "pipeline-unit", (
            "exit event should retain the run ID tag"
        )
    assert "HELLO" in [
        ev.line
        for ev in events
        if ev.phase == "stdout"
        and typ.cast("int", ev.tags["pipeline_stage_index"]) == 1
    ]


def test_observe_emits_stdin_error_event_when_process_closes_stdin_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emit ``stdin_error`` event when the subprocess closes stdin early."""
    builder, catalogue = _python_builder(project_name="observe-stdin-error")
    cmd = builder(
        "-c",
        "import sys; sys.stdin.close(); print('done')",
    )

    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        """Record an emitted execution event."""
        events.append(ev)

    async def fake_write_stdin(
        process: asyncio.subprocess.Process,
        stdin_data: bytes,
        observation: _StageObservation,
    ) -> None:
        """Emit a deterministic stdin error without relying on pipe pressure."""
        await asyncio.sleep(0)
        assert stdin_data == b"x", "stdin writer should receive the configured payload"
        observation.emit(
            "stdin_error",
            _EventDetails(pid=process.pid, note="BrokenPipeError: forced EPIPE"),
        )

    monkeypatch.setattr(_subprocess_stdin, "_write_stdin", fake_write_stdin)
    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        result = cmd.run_sync(
            stdin=StdinInput(data=b"x"),
            output=RunOutputOptions(capture=True),
        )

    assert result.exit_code == 0, (
        f"subprocess exited with non-zero code: {result.exit_code}"
    )
    stdin_error_events = [ev for ev in events if ev.phase == "stdin_error"]
    assert stdin_error_events, (
        "expected at least one stdin_error event when subprocess closes stdin early"
    )
    first = stdin_error_events[0]
    assert first.pid is not None, (
        "expected pid to be present on first stdin_error event"
    )
    assert first.note is not None, (
        "expected error note/detail to be present on first stdin_error event"
    )


@pytest.mark.parametrize(
    ("failing_stdin", "expected_note", "expected_events"),
    [
        pytest.param(
            _FailingStdin(drain_error=OSError("disk quota-ish")),
            "OSError: disk quota-ish",
            [
                (
                    "stdin_error",
                    _EventDetails(pid=123, note="OSError: disk quota-ish"),
                )
            ],
            id="os_error_from_drain",
        ),
        pytest.param(
            _FailingStdin(wait_closed_error=RuntimeError("loop closed")),
            "RuntimeError: loop closed",
            [
                ("stdin", _EventDetails(pid=123, byte_count=7)),
                (
                    "stdin_error",
                    _EventDetails(pid=123, note="RuntimeError: loop closed"),
                ),
            ],
            id="runtime_error_from_wait_closed",
        ),
    ],
)
def test_write_stdin_observes_error_events(
    failing_stdin: _FailingStdin,
    expected_note: str,
    expected_events: list[tuple[str, _EventDetails]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Emit stdin_error details for failures during write/drain/close."""
    process = _FakeProcess(failing_stdin)
    observation = _FakeObservation()

    with caplog.at_level(logging.WARNING, logger="cuprum.stdin"):
        asyncio.run(
            _write_stdin(
                typ.cast("asyncio.subprocess.Process", process),
                b"payload",
                typ.cast("_StageObservation", observation),
            )
        )

    assert failing_stdin.closed is True, (
        "stdin must be closed even when drain/wait_closed fail"
    )
    assert observation.events == expected_events, (
        "stdin writer should emit the expected stdin/stdin_error event sequence"
    )
    assert any(expected_note in record.message for record in caplog.records), (
        "stdin failure should be logged with the same diagnostic note"
    )
