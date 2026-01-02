"""Behavioural tests for stream fidelity through pipelines."""

from __future__ import annotations

import base64
import random
import typing as typ

from pytest_bdd import given, scenario, then, when

from cuprum import scoped, sh
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from tests.helpers.catalogue import cat_program, python_catalogue

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

    from cuprum.program import Program
    from cuprum.sh import Pipeline, PipelineResult

# Fixed seed ensures deterministic output across runs.
_SEED = 20260101
_LINES = 512
_BYTES_PER_LINE = 48  # Results in 64 chars of base64 per line


def _generate_test_data() -> str:
    """Generate deterministic random base64 data for testing.

    Uses a local RNG with fixed seed so the output is identical across runs,
    enabling reliable snapshot comparisons without affecting global state.
    """
    rng = random.Random(_SEED)  # noqa: S311
    lines = []
    for _ in range(_LINES):
        raw = rng.randbytes(_BYTES_PER_LINE)
        lines.append(base64.b64encode(raw).decode("ascii"))
    return "\n".join(lines)


@scenario(
    "../features/stream_fidelity.feature",
    "Pipeline preserves 512 lines of random data",
)
def test_pipeline_preserves_random_data() -> None:
    """Behavioural coverage for stream fidelity through cat."""


@given(
    "512 lines of deterministic random base64 data",
    target_fixture="test_pipeline",
)
def given_random_data() -> tuple[Pipeline, frozenset[Program]]:
    """Generate test data and build the pipeline."""
    data = _generate_test_data()

    # Get programs for the pipeline
    _, python_prog = python_catalogue()
    cat_prog = cat_program()

    # Combine into single catalogue for the pipeline
    project = ProjectSettings(
        name="stream-fidelity-tests",
        programs=(python_prog, cat_prog),
        documentation_locations=("docs/users-guide.md#pipeline-execution",),
        noise_rules=(),
    )
    catalogue = ProgramCatalogue(projects=(project,))

    python_cmd = sh.make(python_prog, catalogue=catalogue)
    cat_cmd = sh.make(cat_prog, catalogue=catalogue)

    # Pipeline: Python outputs data, cat passes it through
    pipeline = python_cmd("-c", f"print({data!r})") | cat_cmd()

    allowlist = frozenset([python_prog, cat_prog])
    return pipeline, allowlist


@when(
    "I pipe the data through cat synchronously",
    target_fixture="pipeline_result",
)
def when_pipe_through_cat(
    test_pipeline: tuple[Pipeline, frozenset[Program]],
) -> PipelineResult:
    """Execute the pipeline synchronously."""
    pipeline, allowlist = test_pipeline
    with scoped(allowlist=allowlist):
        return pipeline.run_sync()


@then("the output matches the snapshot")
def then_output_matches_snapshot(
    pipeline_result: PipelineResult,
    snapshot: SnapshotAssertion,
) -> None:
    """Verify the pipeline output matches the expected snapshot."""
    assert pipeline_result.ok
    assert pipeline_result.stdout == snapshot
