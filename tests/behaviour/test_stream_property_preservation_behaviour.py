"""Behavioural tests for deterministic random stream preservation cases."""

from __future__ import annotations

import dataclasses
import typing as typ

from pytest_bdd import given, parsers, scenario, then, when

from cuprum import sh
from tests.helpers.parity import (
    chunked_writer_script,
    deterministic_property_case,
    hex_sink_script,
    parity_catalogue,
    payload_to_base64,
    run_parity_pipeline,
)

if typ.TYPE_CHECKING:
    from cuprum.program import Program
    from cuprum.sh import Pipeline, PipelineResult


@dataclasses.dataclass(frozen=True, slots=True)
class PropertyScenario:
    """Deterministic stream property test case."""

    pipeline: Pipeline
    allowlist: frozenset[Program]
    expected_hex: str
    chunk_count: int


def _build_property_scenario(
    *,
    seed: int,
    size: int,
) -> PropertyScenario:
    """Create a deterministic property scenario from a seed and payload size."""
    payload, chunk_sizes = deterministic_property_case(seed=seed, payload_size=size)
    catalogue, python_prog, _, _ = parity_catalogue()
    python_cmd = sh.make(python_prog, catalogue=catalogue)

    chunk_spec = ",".join(str(value) for value in chunk_sizes)
    pipeline = python_cmd(
        "-c",
        chunked_writer_script(),
        payload_to_base64(payload),
        chunk_spec,
    ) | python_cmd("-c", hex_sink_script())

    return PropertyScenario(
        pipeline=pipeline,
        allowlist=frozenset([python_prog]),
        expected_hex=payload.hex(),
        chunk_count=len(chunk_sizes),
    )


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
) -> PropertyScenario:
    """Build a deterministic random payload pipeline case.

    Parameters
    ----------
    seed : int
        Random seed used to generate payload and chunk boundaries.
    size : int
        Payload size in bytes.

    Returns
    -------
    PropertyScenario
        Pipeline configuration and expected output for assertions.
    """
    return _build_property_scenario(seed=seed, size=size)


@when(
    "I run the stream property pipeline synchronously",
    target_fixture="pipeline_result",
)
def when_run_property_pipeline(
    property_scenario: PropertyScenario,
) -> PipelineResult:
    """Execute the deterministic stream property pipeline.

    Parameters
    ----------
    property_scenario : PropertyScenario
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
    property_scenario: PropertyScenario,
    pipeline_result: PipelineResult,
) -> None:
    """Assert that downstream output preserves the original payload bytes.

    Parameters
    ----------
    property_scenario : PropertyScenario
        Scenario data containing the expected hex payload.
    pipeline_result : PipelineResult
        Actual execution result.
    """
    assert property_scenario.chunk_count >= 2
    assert pipeline_result.stdout == property_scenario.expected_hex


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
    assert pipeline_result.ok is True
    assert len(pipeline_result.stages) == 2
    assert all(stage.exit_code == 0 for stage in pipeline_result.stages)
