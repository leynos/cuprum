"""End-to-end checks of raw execution and opted-in telemetry projections."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum import ExecutionContext, logging_hook, sh
from cuprum.adapters.logging_adapter import structured_logging_hook
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook
from cuprum.context import ScopeConfig, scoped
from cuprum.redaction import RedactionPolicy, redacting_hook
from cuprum.unittests._adapter_test_support import capturing_logger
from tests.behaviour._telemetry_adapter_support import (
    _REDACTION_SCRIPT,
    PythonCommandFixture,
)

if typ.TYPE_CHECKING:
    import logging

    from cuprum.events import ExecEvent
    from cuprum.sh import CommandResult

_SENTINELS = {
    "argv": "argv-sentinel-442",
    "env": "environment-sentinel-442",
    "stdout": "stdout-sentinel-442",
    "stderr": "stderr-sentinel-442",
    "tag": "tag-sentinel-442",
}


@dc.dataclass(frozen=True, slots=True)
class RedactionRun:
    """Captured results and exports from one command execution.

    Attributes
    ----------
    result:
        Raw command result returned to the caller.
    argv:
        Unchanged command arguments, including the program.
    records:
        Structured and plain logging records retained before formatting.
    tracer:
        Backend retaining projected span data.
    exports:
        Events delivered to the wrapped observation consumer.
    raw_events:
        Events delivered to a separate trusted observer.
    """

    result: CommandResult
    argv: tuple[str, ...]
    records: list[logging.LogRecord]
    tracer: InMemoryTracer
    exports: list[ExecEvent]
    raw_events: list[ExecEvent]


def given_shared_redaction_policy() -> RedactionPolicy:
    """Configure every field sentinel explicitly without automatic detection."""
    return RedactionPolicy(literals=tuple(_SENTINELS.values()))


def when_run_with_shared_redaction(
    behaviour_state: dict[str, object],
    python_cmd_fixture: PythonCommandFixture,
    redaction_policy: RedactionPolicy,
) -> None:
    """Execute one sentinel-producing child with raw and sanitized observers."""
    command = python_cmd_fixture["builder"](
        "-c",
        _REDACTION_SCRIPT,
        _SENTINELS["argv"],
        _SENTINELS["stdout"],
        _SENTINELS["stderr"],
    )
    tracer = InMemoryTracer()
    exports: list[ExecEvent] = []
    raw_events: list[ExecEvent] = []
    execution = ExecutionContext(
        env={"CUPRUM_SENTINEL": _SENTINELS["env"]},
        tags={"credential": _SENTINELS["tag"]},
    )
    with (
        capturing_logger("cuprum.test.redaction.bdd") as capture,
        scoped(ScopeConfig(allowlist=python_cmd_fixture["catalogue"].allowlist)),
        logging_hook(logger=capture.logger, redaction=redaction_policy),
        sh.observe(
            structured_logging_hook(logger=capture.logger, redaction=redaction_policy)
        ),
        sh.observe(TracingHook(tracer, redaction=redaction_policy)),
        sh.observe(redacting_hook(exports.append, redaction_policy)),
        sh.observe(raw_events.append),
    ):
        result = command.run_sync(context=execution)

    behaviour_state["redaction_run"] = RedactionRun(
        result,
        command.argv_with_program,
        capture.records,
        tracer,
        exports,
        raw_events,
    )


def _run_state(behaviour_state: dict[str, object]) -> RedactionRun:
    """Retrieve the completed command and its observed exports."""
    run = behaviour_state["redaction_run"]
    assert isinstance(run, RedactionRun), "the scenario must run its configured child"
    return run


def then_exports_exclude_sentinels(behaviour_state: dict[str, object]) -> None:
    """Verify rendered logs, record attributes, spans, and exports are sanitized."""
    run = _run_state(behaviour_state)
    assert run.records, "the configured logger must produce records"
    assert run.tracer.spans, "the configured tracer must produce spans"
    assert run.exports, "the wrapped observer must receive events"
    assert len(run.exports) == len(run.raw_events), "redaction must not drop events"
    surfaces = {
        "rendered logs": [record.getMessage() for record in run.records],
        "record attributes": [vars(record) for record in run.records],
        "span data": run.tracer.spans,
        "observer exports": run.exports,
    }
    for name, value in surfaces.items():
        rendered = repr(value)
        for sentinel in _SENTINELS.values():
            assert sentinel not in rendered, f"{name} must exclude {sentinel}"


def then_execution_preserves_sentinels(behaviour_state: dict[str, object]) -> None:
    """Verify the child, result, command, and separate observer retain raw values."""
    run = _run_state(behaviour_state)
    expected_stdout = (
        "\n".join(_SENTINELS[field] for field in ("argv", "env", "stdout")) + "\n"
    )
    assert run.result.exit_code == 0, "redaction must preserve successful execution"
    assert run.result.stdout == expected_stdout, "captured stdout must remain unchanged"
    assert run.result.stderr == _SENTINELS["stderr"] + "\n", (
        "captured stderr must remain unchanged"
    )
    assert run.argv[-3:] == tuple(
        _SENTINELS[field] for field in ("argv", "stdout", "stderr")
    ), "the executed arguments must retain the configured values"
    assert all(value in repr(run.raw_events) for value in _SENTINELS.values()), (
        "an unwrapped observer must retain all raw field values"
    )
    assert any(
        event.env is not None and event.env.get("CUPRUM_SENTINEL") == _SENTINELS["env"]
        for event in run.raw_events
    ), "the observed child environment must remain unchanged"
    assert all(
        event.tags["credential"] == _SENTINELS["tag"] for event in run.raw_events
    ), "raw observation tags must remain unchanged"
