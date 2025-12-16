"""Behavioural tests for Pipeline execution."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from pytest_bdd import given, scenario, then, when

from cuprum import ECHO, scoped, sh
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.program import Program
    from cuprum.sh import Pipeline, PipelineResult


@scenario(
    "../features/pipeline_execution.feature",
    "Pipeline streams output between stages",
)
def test_pipeline_streams_between_stages() -> None:
    """Behavioural coverage for pipeline streaming semantics."""


@scenario(
    "../features/pipeline_execution.feature",
    "Pipeline can run asynchronously",
)
def test_pipeline_runs_async() -> None:
    """Behavioural coverage for async pipeline execution."""


@scenario(
    "../features/pipeline_execution.feature",
    "Pipeline reports metadata when a stage fails",
)
def test_pipeline_reports_metadata_on_failure() -> None:
    """Behavioural coverage for failure metadata semantics."""


@dc.dataclass(frozen=True, slots=True)
class _ScenarioPipeline:
    pipeline: Pipeline
    allowlist: frozenset[Program]


def _build_scenario_pipeline_from_commands(
    commands: list[tuple[typ.Any, tuple[str, ...]]],
    allowlist: frozenset[Program],
) -> _ScenarioPipeline:
    """Build a _ScenarioPipeline from command builders and arguments.

    Args:
        commands: List of (command_builder, args_tuple) pairs.
        allowlist: Programs to allow during execution.

    Returns:
        A _ScenarioPipeline with the constructed pipeline and allowlist.

    """
    if len(commands) < 2:
        msg = "Pipeline requires at least two stages"
        raise ValueError(msg)

    # Build pipeline left-to-right
    pipeline = commands[0][0](*commands[0][1])
    for cmd, args in commands[1:]:
        pipeline |= cmd(*args)

    return _ScenarioPipeline(pipeline=pipeline, allowlist=allowlist)


@given("a simple two stage pipeline", target_fixture="pipeline_under_test")
def given_simple_pipeline() -> _ScenarioPipeline:
    """Create a two stage pipeline that uppercases its input."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    echo = sh.make(ECHO)

    return _build_scenario_pipeline_from_commands(
        [
            (echo, ("-n", "behaviour")),
            (python, ("-c", "import sys; sys.stdout.write(sys.stdin.read().upper())")),
        ],
        allowlist=frozenset([ECHO, python_program]),
    )


@given(
    "a two stage pipeline with a failing first stage",
    target_fixture="pipeline_under_test",
)
def given_failing_pipeline() -> _ScenarioPipeline:
    """Create a two stage pipeline where the first stage exits non-zero."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    return _build_scenario_pipeline_from_commands(
        [
            (python, ("-c", "import sys; sys.exit(1)")),
            (python, ("-c", "import sys; sys.exit(0)")),
        ],
        allowlist=frozenset([python_program]),
    )


@when("I run the pipeline synchronously", target_fixture="pipeline_result")
def when_run_pipeline_sync(pipeline_under_test: _ScenarioPipeline) -> PipelineResult:
    """Execute the pipeline via run_sync()."""
    with scoped(allowlist=pipeline_under_test.allowlist):
        return pipeline_under_test.pipeline.run_sync()


@when("I run the pipeline asynchronously", target_fixture="pipeline_result")
def when_run_pipeline_async(pipeline_under_test: _ScenarioPipeline) -> PipelineResult:
    """Execute the pipeline via run()."""

    async def run() -> PipelineResult:
        with scoped(allowlist=pipeline_under_test.allowlist):
            return await pipeline_under_test.pipeline.run()

    return asyncio.run(run())


@then("the pipeline output is transformed")
def then_pipeline_output_transformed(pipeline_result: PipelineResult) -> None:
    """Return transformed stdout from the final stage."""
    assert pipeline_result.stdout == "BEHAVIOUR"


@then("the pipeline exposes per stage exit metadata")
def then_pipeline_exposes_stage_metadata(pipeline_result: PipelineResult) -> None:
    """Stage results include exit codes and process identifiers."""
    assert len(pipeline_result.stages) == 2
    assert all(stage.exit_code == 0 for stage in pipeline_result.stages)
    assert all(stage.pid > 0 for stage in pipeline_result.stages)


@then("the pipeline exposes per stage exit metadata when a stage fails")
def then_pipeline_exposes_stage_metadata_on_failure(
    pipeline_result: PipelineResult,
) -> None:
    """Stage results reflect failure while still reporting metadata."""
    assert len(pipeline_result.stages) == 2
    assert pipeline_result.ok is False
    assert pipeline_result.stages[0].exit_code != 0
    assert pipeline_result.stages[1].exit_code == 0
    assert all(stage.pid > 0 for stage in pipeline_result.stages)
