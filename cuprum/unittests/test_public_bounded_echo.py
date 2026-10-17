"""Public bounded-echo behaviour for commands and pipelines."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import io

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import (
    BoundedDelivery,
    CommandResult,
    ExecutionContext,
    Pipeline,
    PipelineResult,
    RunOutputOptions,
    SafeCmd,
)
from tests.helpers.catalogue import python_catalogue


@dc.dataclass(frozen=True, slots=True)
class _BoundedEchoCase:
    """Shared input and expected output for public bounded-echo tests."""

    payload: str
    bound: int
    expected_echo: str
    sink: io.StringIO
    options: RunOutputOptions


def _bounded_echo_case() -> _BoundedEchoCase:
    """Build the common bounded output options and exact expected echo."""
    payload = "x" * 80 + "\n"
    bound = 50
    return _BoundedEchoCase(
        payload=payload,
        bound=bound,
        expected_echo="x" * 25 + "… [truncated 55 bytes]\n",
        sink=io.StringIO(),
        options=RunOutputOptions(
            capture=True,
            echo=True,
            max_echo_line_bytes=bound,
        ),
    )


def _assert_bounded_public_echo(
    result: CommandResult | PipelineResult,
    case: _BoundedEchoCase,
) -> None:
    """Assert a public execution captures and echoes the shared case exactly."""
    assert result.ok is True, "the public execution should succeed"
    assert result.stdout == case.payload, "capture must retain the complete line"
    assert case.sink.getvalue() == case.expected_echo, (
        "echo must retain the exact bounded prefix, marker, and newline"
    )
    assert len(case.sink.getvalue().encode()) <= case.bound, (
        "the echoed bytes must not exceed max_echo_line_bytes"
    )


def _execute_bounded_subject(
    subject: SafeCmd | Pipeline,
    *,
    is_sync: bool,
    case: _BoundedEchoCase,
) -> CommandResult | PipelineResult:
    """Execute a command or pipeline through its selected public entry point."""
    context = ExecutionContext(stdout_sink=case.sink)
    if is_sync:
        return subject.run_sync(output=case.options, context=context)
    return asyncio.run(subject.run(output=case.options, context=context))


@pytest.mark.parametrize("subject_kind", ["command", "pipeline"])
@pytest.mark.parametrize("is_sync", [False, True], ids=["run", "run-sync"])
def test_public_output_bound_reaches_echo_sink(
    subject_kind: str,
    *,
    is_sync: bool,
) -> None:
    """Public command and pipeline entry points bound echo and retain capture."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    case = _bounded_echo_case()
    producer = python("-c", f"import sys; sys.stdout.write({case.payload!r})")
    subject = (
        producer
        if subject_kind == "command"
        else producer | python("-c", "import sys; sys.stdout.write(sys.stdin.read())")
    )

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = _execute_bounded_subject(subject, is_sync=is_sync, case=case)

    _assert_bounded_public_echo(result, case)


@pytest.mark.parametrize("is_sync", [False, True], ids=["run", "run-sync"])
def test_pipeline_bounded_delivery_uses_shared_worker_for_echoed_streams(
    is_sync: bool,
) -> None:
    """One pipeline worker delivers final stdout and every stage's stderr."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python(
        "-c",
        "import sys; sys.stdout.write('pipeline-out'); "
        "sys.stderr.write('producer-err\\n')",
    )
    consumer = python("-c", "import sys; sys.stdout.write(sys.stdin.read())")
    pipeline = producer | consumer
    sink = io.StringIO()
    options = RunOutputOptions(
        capture=True,
        echo=True,
        delivery=BoundedDelivery(max_queued_bytes=256),
    )
    context = ExecutionContext(stdout_sink=sink, stderr_sink=sink)

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = (
            pipeline.run_sync(output=options, context=context)
            if is_sync
            else asyncio.run(pipeline.run(output=options, context=context))
        )

    assert result.ok is True
    assert result.stdout == "pipeline-out"
    assert result.stages[0].stderr == "producer-err\n"
    assert "pipeline-out" in sink.getvalue()
    assert "producer-err" in sink.getvalue()
    assert all(stage.relay_fallbacks == () for stage in result.stages)
