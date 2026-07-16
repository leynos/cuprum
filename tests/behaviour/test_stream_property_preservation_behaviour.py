"""Behavioural stream-preservation coverage for public pipeline execution.

The canonical ``cuprum._streams._drain`` loop is intentionally exercised here
through ``Pipeline.run_sync`` rather than by importing it directly.  The
behavioural scenario keeps deterministic stream-preservation coverage at the
public API boundary, while the Hypothesis property below drives real subprocess
I/O with randomized payloads and chunk boundaries.  That property verifies the
concurrent final stdout and stderr capture mechanics that wrap ``_drain`` in
normal pipeline execution, including the case where both stream-drain tasks
echo into one shared sink.

The module reuses payload, chunking, and allowlist helpers from
``tests.helpers.parity`` so the behavioural and property tests construct
pipelines the same way as the rest of the stream parity suite.
"""

from __future__ import annotations

import base64
import io
import typing as typ
from collections import Counter

from hypothesis import HealthCheck, settings
from hypothesis import given as hypothesis_given
from hypothesis import strategies as st
from pytest_bdd import given, parsers, scenario, then, when

from cuprum import ExecutionContext, RunOutputOptions, ScopeConfig, scoped, sh
from tests.helpers.parity import (
    PropertyPipelineCase,
    build_property_pipeline_case,
    chunk_sizes_from_cut_points,
    chunked_writer_script,
    deterministic_property_case,
    parity_catalogue,
    payload_to_base64,
    run_parity_pipeline,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import PipelineResult

_CONCURRENT_DRAIN_MAX_EXAMPLES = 8


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
    property_scenario = build_property_pipeline_case(payload, chunk_sizes)
    assert property_scenario.chunk_count >= 2, (
        "Expected at least 2 chunks in property scenario; "
        f"got chunk_count={property_scenario.chunk_count}"
    )
    return property_scenario


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


@st.composite
def _payload_and_chunk_sizes(draw: st.DrawFn) -> tuple[bytes, tuple[int, ...]]:
    """Generate payload bytes and chunk sizes for subprocess pipeline tests."""
    payload = draw(st.binary(min_size=0, max_size=1024))
    payload_size = len(payload)
    if payload_size <= 1:
        return payload, (payload_size,) if payload_size > 0 else ()

    max_cuts = min(8, payload_size - 1)
    cut_points = draw(
        st.lists(
            st.integers(min_value=1, max_value=payload_size - 1),
            min_size=1,
            max_size=max_cuts,
            unique=True,
        ).map(lambda points: tuple(sorted(points))),
    )
    return payload, chunk_sizes_from_cut_points(payload_size, cut_points)


def _stdout_stderr_sink_script() -> str:
    """Return Python code that writes derived data to stdout and stderr."""
    return (
        "import base64\n"
        "import sys\n"
        "data = sys.stdin.buffer.read()\n"
        "stdout = data.hex().encode('ascii')\n"
        "stderr = base64.b64encode(data[::-1])\n"
        "split_stdout = len(stdout) // 2\n"
        "split_stderr = len(stderr) // 2\n"
        "sys.stdout.buffer.write(stdout[:split_stdout])\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(stderr[:split_stderr])\n"
        "sys.stderr.buffer.flush()\n"
        "sys.stdout.buffer.write(stdout[split_stdout:])\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(stderr[split_stderr:])\n"
        "sys.stderr.buffer.flush()\n"
    )


def _run_echoing_stdout_stderr_pipeline(
    payload: bytes,
    chunk_sizes: cabc.Sequence[int],
    sink: io.StringIO,
) -> PipelineResult:
    """Run a public pipeline that captures and echoes stdout plus stderr."""
    catalogue, python_prog, _, _ = parity_catalogue()
    python_cmd = sh.make(python_prog, catalogue=catalogue)
    chunk_spec = ",".join(str(value) for value in chunk_sizes)
    pipeline = python_cmd(
        "-c",
        chunked_writer_script(),
        payload_to_base64(payload),
        chunk_spec,
    ) | python_cmd("-c", _stdout_stderr_sink_script())

    context = ExecutionContext(stdout_sink=sink, stderr_sink=sink)
    with scoped(ScopeConfig(allowlist=frozenset([python_prog]))):
        return pipeline.run_sync(output=RunOutputOptions(echo=True), context=context)


@settings(
    max_examples=_CONCURRENT_DRAIN_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@hypothesis_given(case=_payload_and_chunk_sizes())
def test_pipeline_captures_concurrent_stdout_and_stderr_independently(
    stream_backend: str,
    case: tuple[bytes, tuple[int, ...]],
) -> None:
    """Property: public pipelines capture concurrent stdout and stderr drains.

    Parameters
    ----------
    stream_backend : str
        Active stream backend from fixture parameterization.
    case : tuple[bytes, tuple[int, ...]]
        Random payload and random chunk partition for the upstream process.
    """
    payload, chunk_sizes = case
    sink = io.StringIO()
    result = _run_echoing_stdout_stderr_pipeline(payload, chunk_sizes, sink)

    expected_stdout = payload.hex()
    expected_stderr = base64.b64encode(payload[::-1]).decode("ascii")
    actual_echo = sink.getvalue()
    expected_echo = expected_stdout + expected_stderr

    assert result.stdout == expected_stdout, (
        "Pipeline final stdout must match the hex-encoded payload for "
        f"stream_backend={stream_backend!r}, payload={payload!r}, "
        f"chunk_sizes={chunk_sizes!r}"
    )
    assert result.final.stderr == expected_stderr, (
        "Pipeline final stderr must match the reversed base64 payload for "
        f"stream_backend={stream_backend!r}, payload={payload!r}, "
        f"chunk_sizes={chunk_sizes!r}"
    )
    assert result.ok is True, (
        "Pipeline must complete successfully for concurrent stdout/stderr "
        f"capture; got exit_codes={[stage.exit_code for stage in result.stages]}"
    )
    assert len(actual_echo) == len(expected_echo), (
        "Shared echo sink must receive all characters from stdout and stderr "
        f"for stream_backend={stream_backend!r}, payload={payload!r}, "
        f"chunk_sizes={chunk_sizes!r}, actual_echo={actual_echo!r}, "
        f"expected_echo={expected_echo!r}"
    )
    assert Counter(actual_echo) == Counter(expected_echo), (
        "Shared echo sink must receive a permutation of stdout and stderr "
        f"for stream_backend={stream_backend!r}, payload={payload!r}, "
        f"chunk_sizes={chunk_sizes!r}, actual_echo={actual_echo!r}, "
        f"expected_echo={expected_echo!r}"
    )
