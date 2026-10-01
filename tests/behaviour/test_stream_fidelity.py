"""Behavioural tests for stream fidelity through pipelines."""

from __future__ import annotations

import base64
import dataclasses as dc
import random
import typing as typ

from pytest_bdd import given, scenario, then, when

from cuprum import ScopeConfig, scoped, sh
from tests.helpers.catalogue import (
    cat_program,
    combine_programs_into_catalogue,
    python_catalogue,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

    from cuprum.program import Program
    from cuprum.sh import BytesPipelineResult, Pipeline, PipelineResult

# Fixed seed ensures deterministic output across runs.
_SEED = 20260101
_LINES = 512
_BYTES_PER_LINE = 48  # Results in 64 chars of base64 per line


@dc.dataclass(frozen=True, slots=True)
class _BinaryCase:
    """A byte-exact pipeline, the payload it relays, and its allowlist."""

    pipeline: Pipeline
    payload: bytes
    allowlist: frozenset[Program]


def _generate_test_data() -> str:
    """Generate deterministic random base64 data for testing."""
    # Use a local RNG with a fixed seed so the output is identical across runs,
    # enabling reliable snapshot comparisons without affecting global state.
    rng = random.Random(_SEED)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
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
    """Generate test data and build the pipeline.

    Returns
    -------
    tuple[Pipeline, frozenset[Program]]
        A tuple containing:
        - Pipeline: The composed python->cat pipeline that prints the
          generated base64 data via Python and pipes it through cat.
        - frozenset[Program]: The allowlist of programs (python_prog and
          cat_prog) required by scoped(ScopeConfig()) to permit execution.

    """
    data = _generate_test_data()

    # Get programs for the pipeline
    _, python_prog = python_catalogue()
    cat_prog = cat_program()

    # Combine into single catalogue for the pipeline
    catalogue = combine_programs_into_catalogue(
        python_prog,
        cat_prog,
        project_name="stream-fidelity-tests",
        documentation_locations=("docs/users-guide.md#connect-a-pipeline",),
    )

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
    """Execute the pipeline synchronously.

    Parameters
    ----------
    test_pipeline : tuple[Pipeline, frozenset[Program]]
        A tuple containing the composed python->cat pipeline and the
        allowlist of programs it requires for execution under
        ``scoped(ScopeConfig())``.

    Returns
    -------
    PipelineResult
        The result of running the pipeline.
    """
    pipeline, allowlist = test_pipeline
    with scoped(ScopeConfig(allowlist=allowlist)):
        return pipeline.run_sync()


@then("the output matches the snapshot")
def then_output_matches_snapshot(
    pipeline_result: PipelineResult,
    snapshot: SnapshotAssertion,
) -> None:
    """Verify the pipeline output matches the expected snapshot."""
    assert pipeline_result.ok, "Expected pipeline_result.ok"
    assert pipeline_result.stdout == snapshot, (
        "Expected pipeline_result.stdout == snapshot"
    )


@scenario(
    "../features/stream_fidelity.feature",
    "Pipeline preserves bytes that are not valid text",
)
def test_pipeline_preserves_binary_data() -> None:
    """Behavioural coverage for byte-exact fidelity through cat."""


@given(
    "a binary payload no decoder can round-trip",
    target_fixture="binary_pipeline",
)
def given_binary_payload() -> _BinaryCase:
    """Build a pipeline whose payload survives only if nothing decodes it.

    Every byte value is followed by a lone continuation byte, a byte no UTF-8
    sequence starts with, and a NUL. A text-mode run of this payload would
    replace the three invalid bytes, so the capture can only match if the
    pipeline relayed and captured it without decoding.

    Returns
    -------
    _BinaryCase
        The composed python->cat pipeline, the payload it relays, and the
        allowlist the scoped run requires.
    """
    payload = bytes(range(256)) + b"\xff\x00\xfe\x80"
    _, python_prog = python_catalogue()
    cat_prog = cat_program()
    catalogue = combine_programs_into_catalogue(
        python_prog,
        cat_prog,
        project_name="stream-fidelity-tests",
        documentation_locations=("docs/users-guide.md#binary-output",),
    )
    python_cmd = sh.make(python_prog, catalogue=catalogue)
    cat_cmd = sh.make(cat_prog, catalogue=catalogue)
    producer = (
        f"import sys; sys.stdout.buffer.write({payload!r});sys.stdout.buffer.flush()"
    )
    return _BinaryCase(
        pipeline=python_cmd("-c", producer) | cat_cmd(),
        payload=payload,
        allowlist=frozenset([python_prog, cat_prog]),
    )


@when(
    "I pipe the payload through cat byte-exactly",
    target_fixture="binary_result",
)
def when_pipe_binary_through_cat(binary_pipeline: _BinaryCase) -> BytesPipelineResult:
    """Execute the pipeline through the byte-exact entry point.

    Returns
    -------
    BytesPipelineResult
        The run's result, whose ``stdout`` must be the payload unchanged.
    """
    with scoped(ScopeConfig(allowlist=binary_pipeline.allowlist)):
        return binary_pipeline.pipeline.run_bytes_sync()


@then("the captured bytes are the payload unchanged")
def then_captured_bytes_match(
    binary_result: BytesPipelineResult,
    binary_pipeline: _BinaryCase,
) -> None:
    """Assert the relayed capture is the payload, byte for byte."""
    assert binary_result.ok is True, "every stage should exit cleanly"
    assert binary_result.stdout == binary_pipeline.payload, (
        "the relayed capture must be the payload unchanged, got "
        f"{binary_result.stdout!r}"
    )
