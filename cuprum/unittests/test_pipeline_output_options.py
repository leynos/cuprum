"""Unit tests for Pipeline output option resolution and execution."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import io
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import ECHO, Program, ScopeConfig, scoped, sh
from cuprum.sh import (
    ExecutionContext,
    Pipeline,
    PipelineResult,
    RunOutputOptions,
    _resolve_pipeline_output,
)
from tests.helpers.catalogue import python_catalogue

type PipelineExecuteFn = cabc.Callable[[Pipeline, dict[str, typ.Any]], PipelineResult]


def _execute_async(pipeline: Pipeline, kwargs: dict[str, typ.Any]) -> PipelineResult:
    """Execute a Pipeline using the async run() method."""
    return asyncio.run(pipeline.run(**kwargs))


def _execute_sync(pipeline: Pipeline, kwargs: dict[str, typ.Any]) -> PipelineResult:
    """Execute a Pipeline using the sync run_sync() method."""
    return pipeline.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def pipeline_execution_strategy(
    request: pytest.FixtureRequest,
) -> tuple[str, PipelineExecuteFn]:
    """Provide Pipeline execution strategies for run() and run_sync()."""
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


def _identity_pipeline() -> tuple[Pipeline, frozenset[Program]]:
    """Build a two-stage pipeline that forwards stdin to stdout."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    echo = sh.make(ECHO)
    pipeline = echo("-n", "echoed") | python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )
    return pipeline, frozenset([ECHO, python_program])


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_output_options_echo_for_run_and_run_sync(
    pipeline_execution_strategy: tuple[str, PipelineExecuteFn],
) -> None:
    """Pipeline run() and run_sync() honour output echo options."""
    _, execute = pipeline_execution_strategy
    pipeline, allowlist = _identity_pipeline()
    stdout_sink = io.StringIO()

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = execute(
            pipeline,
            {
                "output": RunOutputOptions(capture=True, echo=True),
                "context": ExecutionContext(stdout_sink=stdout_sink),
            },
        )

    assert result.ok is True
    assert result.stdout == "echoed"
    assert stdout_sink.getvalue() == "echoed"


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_async_flat_capture_echo_kwargs_are_deprecated() -> None:
    """Async Pipeline.run() accepts deprecated flat kwargs and warns."""
    pipeline, allowlist = _identity_pipeline()
    stdout_sink = io.StringIO()

    with (
        scoped(ScopeConfig(allowlist=allowlist)),
        pytest.warns(DeprecationWarning, match="RunOutputOptions"),
    ):
        result = asyncio.run(
            pipeline.run(
                capture=True,
                echo=True,
                context=ExecutionContext(stdout_sink=stdout_sink),
            ),
        )

    assert result.ok is True
    assert result.stdout == "echoed"
    assert stdout_sink.getvalue() == "echoed"


_OUTPUT_OPTIONS = st.one_of(
    st.none(),
    st.builds(
        RunOutputOptions,
        capture=st.booleans(),
        echo=st.booleans(),
    ),
)
_DEPRECATED_FLAGS = st.fixed_dictionaries(
    {},
    optional={
        "capture": st.booleans(),
        "echo": st.booleans(),
    },
)


@settings(max_examples=50, deadline=None, derandomize=True)
@given(output=_OUTPUT_OPTIONS, flags=_DEPRECATED_FLAGS)
def test_resolve_pipeline_output_preserves_option_invariants(
    output: RunOutputOptions | None,
    flags: dict[str, bool],
) -> None:
    """Pipeline output resolution preserves the finite option invariants."""
    if output is not None and flags:
        with pytest.raises(ValueError, match="not both"):
            _resolve_pipeline_output(output, flags)
        return

    if flags:
        with pytest.warns(DeprecationWarning, match="RunOutputOptions"):
            resolved = _resolve_pipeline_output(output, flags)
        assert resolved.capture is flags.get("capture", True)
        assert resolved.echo is flags.get("echo", False)
        return

    resolved = _resolve_pipeline_output(output, flags)
    assert resolved == (output or RunOutputOptions())
