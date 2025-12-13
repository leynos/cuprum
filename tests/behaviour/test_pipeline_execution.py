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


@dc.dataclass(frozen=True, slots=True)
class _ScenarioPipeline:
    pipeline: Pipeline
    allowlist: frozenset[Program]


@given("a simple two stage pipeline", target_fixture="simple_pipeline")
def given_simple_pipeline() -> _ScenarioPipeline:
    """Create a two stage pipeline that uppercases its input."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    echo = sh.make(ECHO)

    pipeline = echo("-n", "behaviour") | python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read().upper())",
    )
    allowlist = frozenset([ECHO, python_program])
    return _ScenarioPipeline(pipeline=pipeline, allowlist=allowlist)


@when("I run the pipeline synchronously", target_fixture="pipeline_result")
def when_run_pipeline_sync(simple_pipeline: _ScenarioPipeline) -> PipelineResult:
    """Execute the pipeline via run_sync()."""
    with scoped(allowlist=simple_pipeline.allowlist):
        return simple_pipeline.pipeline.run_sync()


@when("I run the pipeline asynchronously", target_fixture="pipeline_result")
def when_run_pipeline_async(simple_pipeline: _ScenarioPipeline) -> PipelineResult:
    """Execute the pipeline via run()."""

    async def run() -> PipelineResult:
        with scoped(allowlist=simple_pipeline.allowlist):
            return await simple_pipeline.pipeline.run()

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
