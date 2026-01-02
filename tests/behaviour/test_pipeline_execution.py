"""Behavioural tests for Pipeline execution."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from pytest_bdd import given, scenario, then, when

from cuprum import ECHO, scoped, sh
from tests.helpers.catalogue import combine_programs_into_catalogue, python_catalogue

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


@scenario(
    "../features/pipeline_execution.feature",
    "Pipeline fails fast when a middle stage fails",
)
def test_pipeline_fails_fast_on_middle_stage_failure() -> None:
    """Behavioural coverage for middle-stage fail-fast semantics."""


@scenario(
    "../features/pipeline_execution.feature",
    "Pipeline surfaces the failing stage when the final stage fails",
)
def test_pipeline_surfaces_failure_on_final_stage_failure() -> None:
    """Behavioural coverage for final-stage failure reporting semantics."""


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


def _make_test_pipeline(
    *stage_specs: tuple[Program, tuple[str, ...]],
) -> _ScenarioPipeline:
    """Build a test pipeline from program and argument specifications.

    Args:
        stage_specs: Each spec is (program, args_tuple) defining one stage.
                    Program paths are resolved via python_catalogue if needed.

    Returns:
        A _ScenarioPipeline ready for execution with appropriate allowlist.

    """
    _, python_program = python_catalogue()

    # Build a combined catalogue containing both ECHO and Python
    catalogue = combine_programs_into_catalogue(
        ECHO,
        python_program,
        project_name="pipeline-tests",
    )

    # Map programs to command builders using the combined catalogue
    builders: dict[Program, typ.Any] = {
        ECHO: sh.make(ECHO, catalogue=catalogue),
        python_program: sh.make(python_program, catalogue=catalogue),
    }

    # Build commands from specs
    commands = []
    programs_used: set[Program] = set()
    for prog, args in stage_specs:
        if prog not in builders:
            builders[prog] = sh.make(prog, catalogue=catalogue)
        commands.append((builders[prog], args))
        programs_used.add(prog)

    allowlist = frozenset(programs_used)
    return _build_scenario_pipeline_from_commands(commands, allowlist)


@given("a simple two stage pipeline", target_fixture="pipeline_under_test")
def given_simple_pipeline() -> _ScenarioPipeline:
    """Create a two stage pipeline that uppercases its input."""
    _, python_program = python_catalogue()
    return _make_test_pipeline(
        (ECHO, ("-n", "behaviour")),
        (
            python_program,
            ("-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"),
        ),
    )


@given(
    "a three stage pipeline with a failing first stage",
    target_fixture="pipeline_under_test",
)
def given_failing_pipeline() -> _ScenarioPipeline:
    """Create a three stage pipeline where the first stage exits non-zero."""
    _, python_program = python_catalogue()
    return _make_test_pipeline(
        (python_program, ("-c", "import sys; sys.exit(7)")),
        (python_program, ("-c", "import time, sys; time.sleep(2); sys.exit(0)")),
        (python_program, ("-c", "import time, sys; time.sleep(2); sys.exit(0)")),
    )


@given(
    "a three stage pipeline with a failing middle stage",
    target_fixture="pipeline_under_test",
)
def given_middle_stage_failure_pipeline() -> _ScenarioPipeline:
    """Create a three stage pipeline where the middle stage exits non-zero."""
    _, python_program = python_catalogue()
    return _make_test_pipeline(
        (python_program, ("-c", "import time, sys; time.sleep(2); sys.exit(0)")),
        (python_program, ("-c", "import sys; sys.exit(3)")),
        (python_program, ("-c", "import time, sys; time.sleep(2); sys.exit(0)")),
    )


@given(
    "a three stage pipeline with a failing final stage",
    target_fixture="pipeline_under_test",
)
def given_final_stage_failure_pipeline() -> _ScenarioPipeline:
    """Create a three stage pipeline where the final stage exits non-zero."""
    _, python_program = python_catalogue()
    return _make_test_pipeline(
        (python_program, ("-c", "import sys; sys.exit(0)")),
        (python_program, ("-c", "import sys; sys.exit(0)")),
        (python_program, ("-c", "import sys; sys.exit(5)")),
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


@then("the pipeline exposes per stage exit metadata when a stage fails fast")
def then_pipeline_exposes_stage_metadata_on_fail_fast_failure(
    pipeline_result: PipelineResult,
) -> None:
    """Stage results reflect fail-fast termination and surface the failure."""
    assert len(pipeline_result.stages) == 3
    assert pipeline_result.ok is False
    assert pipeline_result.failure is not None
    assert pipeline_result.failure.exit_code != 0
    assert pipeline_result.failure_index is not None
    assert pipeline_result.failure_index < len(pipeline_result.stages) - 1
    assert all(
        stage.exit_code != 0
        for stage in pipeline_result.stages[pipeline_result.failure_index + 1 :]
    )
    assert all(stage.pid > 0 for stage in pipeline_result.stages)


@then("the pipeline exposes per stage exit metadata when the final stage fails")
def then_pipeline_exposes_stage_metadata_on_final_stage_failure(
    pipeline_result: PipelineResult,
) -> None:
    """Stage results surface the failing final stage while still reporting metadata."""
    assert len(pipeline_result.stages) == 3
    assert pipeline_result.ok is False
    assert pipeline_result.failure is pipeline_result.stages[-1]
    assert pipeline_result.failure_index == len(pipeline_result.stages) - 1
    assert pipeline_result.stages[0].exit_code == 0
    assert pipeline_result.stages[1].exit_code == 0
    assert pipeline_result.stages[2].exit_code != 0
    assert all(stage.pid > 0 for stage in pipeline_result.stages)
