"""Behavioural tests for stream backend parity across edge cases.

These BDD scenarios verify that both the Python and Rust stream backends
produce identical pipeline output for edge cases: empty streams, multi-byte
UTF-8, broken pipes, and backpressure.
"""

from __future__ import annotations

import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum import sh
from tests.helpers.parity import (
    parity_catalogue,
    run_parity_pipeline,
    utf8_stress_payload,
)

if typ.TYPE_CHECKING:
    from cuprum.program import Program
    from cuprum.sh import Pipeline, PipelineResult

_LARGE_PAYLOAD_SIZE = 1024 * 1024


@pytest.fixture(scope="session")
def utf8_payload() -> str:
    """Session-scoped UTF-8 stress payload.

    Returns
    -------
    str
        Deterministic multi-byte UTF-8 string exceeding 80 KB encoded.
    """
    return utf8_stress_payload()


@pytest.fixture(scope="session")
def large_payload() -> str:
    """Session-scoped 1 MB payload of repeated ``x`` characters.

    Returns
    -------
    str
        A string of 1,048,576 ``x`` characters.
    """
    return "x" * _LARGE_PAYLOAD_SIZE


# -- Scenarios ----------------------------------------------------------------


@scenario(
    "../features/stream_parity.feature",
    "Empty stream produces identical output across backends",
)
def test_empty_stream_parity(stream_backend: str) -> None:
    """Empty stream produces identical output across backends.

    Parameters
    ----------
    stream_backend : str
        The active stream backend (injected by fixture).
    """


@scenario(
    "../features/stream_parity.feature",
    "Multi-byte UTF-8 survives pipeline across backends",
)
def test_utf8_parity(stream_backend: str) -> None:
    """Multi-byte UTF-8 survives pipeline across backends.

    Parameters
    ----------
    stream_backend : str
        The active stream backend (injected by fixture).
    """


@scenario(
    "../features/stream_parity.feature",
    "Broken pipe is handled gracefully across backends",
)
def test_broken_pipe_parity(stream_backend: str) -> None:
    """Broken pipe is handled gracefully across backends.

    Parameters
    ----------
    stream_backend : str
        The active stream backend (injected by fixture).
    """


@scenario(
    "../features/stream_parity.feature",
    "Large payload survives backpressure across backends",
)
def test_backpressure_parity(stream_backend: str) -> None:
    """Large payload survives backpressure across backends.

    Parameters
    ----------
    stream_backend : str
        The active stream backend (injected by fixture).
    """


# -- Given steps --------------------------------------------------------------


@given(
    "an empty stream pipeline",
    target_fixture="parity_pipeline",
)
def given_empty_stream() -> tuple[Pipeline, frozenset[Program]]:
    """Build a pipeline where the upstream produces no output.

    Returns
    -------
    tuple[Pipeline, frozenset[Program]]
        The pipeline and the allowlist of programmes required.
    """
    catalogue, python_prog, cat_prog, _ = parity_catalogue()
    python_cmd = sh.make(python_prog, catalogue=catalogue)
    cat_cmd = sh.make(cat_prog, catalogue=catalogue)

    pipeline = python_cmd("-c", "pass") | cat_cmd()
    allowlist = frozenset([python_prog, cat_prog])
    return pipeline, allowlist


@given(
    "a pipeline producing multi-byte UTF-8 data",
    target_fixture="parity_pipeline",
)
def given_utf8_pipeline(
    utf8_payload: str,
) -> tuple[Pipeline, frozenset[Program]]:
    """Build a pipeline that emits multi-byte UTF-8 data.

    Parameters
    ----------
    utf8_payload : str
        Session-scoped UTF-8 stress payload fixture.

    Returns
    -------
    tuple[Pipeline, frozenset[Program]]
        The pipeline and the allowlist of programmes required.
    """
    catalogue, python_prog, cat_prog, _ = parity_catalogue()
    python_cmd = sh.make(python_prog, catalogue=catalogue)
    cat_cmd = sh.make(cat_prog, catalogue=catalogue)

    # Write payload as raw UTF-8 bytes to avoid Python's own
    # buffering/encoding layer adding any transformations.
    script = (
        "import sys; "
        f"sys.stdout.buffer.write({utf8_payload!r}.encode('utf-8')); "
        "sys.stdout.buffer.flush()"
    )
    pipeline = python_cmd("-c", script) | cat_cmd()
    allowlist = frozenset([python_prog, cat_prog])
    return pipeline, allowlist


@given(
    "a pipeline with an early-exiting downstream",
    target_fixture="parity_pipeline",
)
def given_broken_pipe() -> tuple[Pipeline, frozenset[Program]]:
    """Build a pipeline where the downstream exits early.

    The upstream writes 64 KB of data whilst the downstream reads only
    10 bytes and exits, triggering a broken pipe condition on the
    inter-stage pump.

    Returns
    -------
    tuple[Pipeline, frozenset[Program]]
        The pipeline and the allowlist of programmes required.
    """
    catalogue, python_prog, _, _ = parity_catalogue()
    python_cmd = sh.make(python_prog, catalogue=catalogue)

    # Upstream: write 64 KB, handle SIGPIPE gracefully.
    upstream_script = (
        "import sys, signal; "
        "signal.signal(signal.SIGPIPE, signal.SIG_DFL); "
        "sys.stdout.buffer.write(b'A' * 65536); "
        "sys.stdout.buffer.flush()"
    )
    # Downstream: read 10 bytes and exit immediately.
    downstream_script = "import sys; sys.stdout.write(sys.stdin.read(10))"
    pipeline = python_cmd("-c", upstream_script) | python_cmd("-c", downstream_script)
    allowlist = frozenset([python_prog])
    return pipeline, allowlist


@given(
    "a three stage pipeline with a one megabyte payload",
    target_fixture="parity_pipeline",
)
def given_backpressure() -> tuple[Pipeline, frozenset[Program]]:
    """Build a three-stage pipeline with a 1 MB payload.

    Returns
    -------
    tuple[Pipeline, frozenset[Program]]
        The pipeline and the allowlist of programmes required.
    """
    catalogue, python_prog, cat_prog, _ = parity_catalogue()
    python_cmd = sh.make(python_prog, catalogue=catalogue)
    cat_cmd = sh.make(cat_prog, catalogue=catalogue)

    # Generate the payload inside the subprocess to avoid exceeding
    # the OS command-line length limit.
    script = f"import sys; sys.stdout.write('x' * {_LARGE_PAYLOAD_SIZE})"
    pipeline = python_cmd("-c", script) | cat_cmd() | cat_cmd()
    allowlist = frozenset([python_prog, cat_prog])
    return pipeline, allowlist


# -- When steps ---------------------------------------------------------------


@when(
    "I run the parity pipeline synchronously",
    target_fixture="pipeline_result",
)
def when_run_sync(
    parity_pipeline: tuple[Pipeline, frozenset[Program]],
) -> PipelineResult:
    """Execute the pipeline via ``run_sync()``.

    Parameters
    ----------
    parity_pipeline : tuple[Pipeline, frozenset[Program]]
        The pipeline and its required allowlist.

    Returns
    -------
    PipelineResult
        The result of synchronous pipeline execution.
    """
    pipeline, allowlist = parity_pipeline
    return run_parity_pipeline(pipeline, allowlist)


# -- Then steps ---------------------------------------------------------------


def _assert_stdout_matches(
    pipeline_result: PipelineResult,
    expected: str,
    description: str,
) -> None:
    """Assert that pipeline stdout matches the expected value.

    Parameters
    ----------
    pipeline_result : PipelineResult
        The result from pipeline execution.
    expected : str
        The expected stdout content.
    description : str
        Human-readable description of the payload for error messages.
    """
    assert pipeline_result.stdout == expected, (
        f"expected {description} to survive pipeline intact"
    )


@then("the stdout is empty")
def then_stdout_empty(pipeline_result: PipelineResult) -> None:
    """Assert that pipeline stdout is an empty string.

    Parameters
    ----------
    pipeline_result : PipelineResult
        The result from pipeline execution.
    """
    _assert_stdout_matches(pipeline_result, "", "empty stdout")


@then("the pipeline succeeded")
def then_pipeline_ok(pipeline_result: PipelineResult) -> None:
    """Assert that all pipeline stages exited with code zero.

    Parameters
    ----------
    pipeline_result : PipelineResult
        The result from pipeline execution.
    """
    assert pipeline_result.ok is True, "pipeline result should indicate success"
    assert all(stage.exit_code == 0 for stage in pipeline_result.stages), (
        "every stage should exit with code 0"
    )


@then("the stdout matches the expected UTF-8 payload")
def then_stdout_utf8(
    pipeline_result: PipelineResult,
    utf8_payload: str,
) -> None:
    """Assert that pipeline stdout matches the UTF-8 stress payload.

    Parameters
    ----------
    pipeline_result : PipelineResult
        The result from pipeline execution.
    utf8_payload : str
        Session-scoped UTF-8 stress payload fixture.
    """
    _assert_stdout_matches(pipeline_result, utf8_payload, "UTF-8 payload")


@then("the pipeline completed without hanging")
def then_no_hang(pipeline_result: PipelineResult) -> None:
    """Assert that the pipeline completed without deadlock.

    The fact that execution reached this assertion proves no deadlock
    occurred (pytest-timeout would have killed the test otherwise).

    Parameters
    ----------
    pipeline_result : PipelineResult
        The result from pipeline execution.
    """
    assert pipeline_result.stages is not None, (
        "pipeline should have produced stage results"
    )
    assert len(pipeline_result.stages) >= 2, "pipeline should have at least two stages"


@then("the downstream stdout matches the first 10 bytes")
def then_stdout_first_10(pipeline_result: PipelineResult) -> None:
    """Assert that pipeline stdout contains the first 10 bytes from upstream.

    The downstream reads exactly 10 bytes from the upstream's 64 KB
    output before exiting.

    Parameters
    ----------
    pipeline_result : PipelineResult
        The result from pipeline execution.
    """
    _assert_stdout_matches(pipeline_result, "A" * 10, "broken-pipe first 10 bytes")


@then("the stdout matches the expected large payload")
def then_stdout_large(
    pipeline_result: PipelineResult,
    large_payload: str,
) -> None:
    """Assert that pipeline stdout matches the 1 MB payload.

    Parameters
    ----------
    pipeline_result : PipelineResult
        The result from pipeline execution.
    large_payload : str
        Session-scoped 1 MB payload fixture.
    """
    _assert_stdout_matches(pipeline_result, large_payload, "1 MB payload")
