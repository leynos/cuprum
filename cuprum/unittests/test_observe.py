"""Unit tests for structured execution events and observe hooks."""

from __future__ import annotations

import sys
import typing as typ
from pathlib import Path

from cuprum import sh
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.context import current_context, scoped
from cuprum.program import Program
from cuprum.sh import ExecutionContext

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent


def _python_builder(
    *, project_name: str = "observe-tests"
) -> tuple[typ.Callable[..., sh.SafeCmd], ProgramCatalogue]:
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

    with scoped(allowlist=allowlist), sh.observe(hook):
        result = cmd.run_sync(context=context)
    return result, events


def test_observe_registration_detaches_cleanly() -> None:
    """observe() registers and detaches from the current context."""
    builder, catalogue = _python_builder()
    cmd = builder("-c", "print('x')")
    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        events.append(ev)

    with scoped(allowlist=catalogue.allowlist):
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
