"""Behavioural tests for deterministic random stream preservation cases."""

from __future__ import annotations

import typing as typ

from pytest_bdd import given, parsers, scenario, then, when

from tests.helpers.parity import (
    PropertyPipelineCase,
    build_property_pipeline_case,
    deterministic_property_case,
    run_parity_pipeline,
)

if typ.TYPE_CHECKING:
    from cuprum.sh import PipelineResult


@scenario(
    "../features/stream_property_preservation.feature",
    "Deterministic random payload preserves bytes across chunk boundaries",
)
def test_stream_property_preservation(stream_backend: str) -> None:
    """Pipeline preserves deterministic random bytes for each example row.

    Parameters
    ----------
    stream_backend : str
        Active stream backend fixture parameter.
    """


@given(
    parsers.parse(
        "a deterministic random payload with seed {seed:d} and size {size:d}",
    ),
    target_fixture="property_scenario",
)
def given_deterministic_payload(
    seed: int,
    size: int,
) -> PropertyPipelineCase:
    """Build a deterministic random payload pipeline case.

    Parameters
    ----------
    seed : int
        Random seed used to generate payload and chunk boundaries.
    size : int
        Payload size in bytes.

    Returns
    -------
    PropertyPipelineCase
        Pipeline configuration and expected output for assertions.
    """
    payload, chunk_sizes = deterministic_property_case(seed=seed, payload_size=size)
    return build_property_pipeline_case(payload, chunk_sizes)


@when(
    "I run the stream property pipeline synchronously",
    target_fixture="pipeline_result",
)
def when_run_property_pipeline(
    property_scenario: PropertyPipelineCase,
) -> PipelineResult:
    """Execute the deterministic stream property pipeline.

    Parameters
    ----------
    property_scenario : PropertyPipelineCase
        Scenario data created in the Given step.

    Returns
    -------
    PipelineResult
        Synchronous pipeline execution result.
    """
    return run_parity_pipeline(
        property_scenario.pipeline,
        property_scenario.allowlist,
    )


@then("the stream property output matches the expected hex payload")
def then_hex_matches(
    property_scenario: PropertyPipelineCase,
    pipeline_result: PipelineResult,
) -> None:
    """Assert that downstream output preserves the original payload bytes.

    Parameters
    ----------
    property_scenario : PropertyPipelineCase
        Scenario data containing the expected hex payload.
    pipeline_result : PipelineResult
        Actual execution result.
    """
    assert property_scenario.chunk_count >= 2, (
        "Expected at least 2 chunks in property scenario; "
        f"got chunk_count={property_scenario.chunk_count}"
    )
    assert pipeline_result.stdout == property_scenario.expected_hex, (
        "Expected pipeline stdout to match hex-encoded source payload; "
        f"got stdout={pipeline_result.stdout!r}"
    )


@then("the pipeline completes successfully")
def then_pipeline_succeeds(
    pipeline_result: PipelineResult,
) -> None:
    """Assert successful execution for all pipeline stages.

    Parameters
    ----------
    pipeline_result : PipelineResult
        Actual execution result.
    """
    assert pipeline_result.ok is True, "Expected pipeline result to be successful"
    assert len(pipeline_result.stages) == 2, (
        "Expected exactly 2 pipeline stages; "
        f"got stage_count={len(pipeline_result.stages)}"
    )
    assert all(stage.exit_code == 0 for stage in pipeline_result.stages), (
        "Expected all pipeline stages to exit with code 0; "
        f"got exit_codes={[stage.exit_code for stage in pipeline_result.stages]}"
    )
