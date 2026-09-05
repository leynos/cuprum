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
from cuprum._constants import DEFAULT_ECHO_MAX_LINE_BYTES
from cuprum.sh import (
    ExecutionContext,
    Pipeline,
    PipelineResult,
    RunOutputOptions,
    _DeprecatedOutputFlags,
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
    """Provide Pipeline execution strategies for run() and run_sync().

    Parameters
    ----------
    request : pytest.FixtureRequest
        Fixture request whose ``param`` selects the asynchronous or
        synchronous execution strategy.

    Returns
    -------
    tuple[str, PipelineExecuteFn]
        The strategy label and its execution callable.
    """
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


def _assert_echoed_and_captured(
    result: PipelineResult, sink: io.StringIO, expected: str
) -> None:
    """Assert a pipeline both captured and echoed the expected output.

    Parameters
    ----------
    result:
        Pipeline result whose success and captured output are asserted.
    sink:
        Text sink expected to contain the echoed output.
    expected:
        Output expected from both capture and echo.
    """
    assert result.ok is True, "the pipeline should succeed"
    assert result.stdout == expected, "capture must return the final stage output"
    assert sink.getvalue() == expected, "echo must also tee the output to the sink"


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

    _assert_echoed_and_captured(result, stdout_sink, "echoed")


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_flat_capture_echo_kwargs_are_deprecated_for_public_entrypoints(
    pipeline_execution_strategy: tuple[str, PipelineExecuteFn],
) -> None:
    """Pipeline run() and run_sync() accept deprecated flat kwargs and warn."""
    _, execute = pipeline_execution_strategy
    pipeline, allowlist = _identity_pipeline()
    stdout_sink = io.StringIO()

    with (
        scoped(ScopeConfig(allowlist=allowlist)),
        pytest.warns(DeprecationWarning, match="RunOutputOptions"),
    ):
        result = execute(
            pipeline,
            {
                "capture": True,
                "echo": True,
                "context": ExecutionContext(stdout_sink=stdout_sink),
            },
        )

    _assert_echoed_and_captured(result, stdout_sink, "echoed")


_OUTPUT_OPTIONS = st.one_of(
    st.none(),
    st.builds(
        RunOutputOptions,
        capture=st.booleans(),
        echo=st.booleans(),
        max_echo_line_bytes=st.one_of(
            st.none(),
            st.integers(min_value=1, max_value=1 << 20),
        ),
    ),
)


def _as_deprecated_flags(raw: cabc.Mapping[str, bool]) -> _DeprecatedOutputFlags:
    """Narrow a generated capture/echo mapping to the keyword TypedDict."""
    flags = _DeprecatedOutputFlags()
    if "capture" in raw:
        flags["capture"] = raw["capture"]
    if "echo" in raw:
        flags["echo"] = raw["echo"]
    return flags


_DEPRECATED_FLAGS = st.fixed_dictionaries(
    {},
    optional={
        "capture": st.booleans(),
        "echo": st.booleans(),
    },
).map(_as_deprecated_flags)


@settings(max_examples=50, deadline=None, derandomize=True)
@given(output=_OUTPUT_OPTIONS, flags=_DEPRECATED_FLAGS)
def test_resolve_pipeline_output_preserves_option_invariants(
    output: RunOutputOptions | None,
    flags: _DeprecatedOutputFlags,
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
    assert resolved == (output or RunOutputOptions()), (
        "omitted flags must resolve to the supplied or default options"
    )


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_run_sync_accepts_run_output_options() -> None:
    """Pipeline.run_sync accepts ``output=RunOutputOptions`` like SafeCmd."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    echo = sh.make(ECHO)

    pipeline = echo("-n", "unified") | python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )

    with scoped(ScopeConfig(allowlist=frozenset([ECHO, python_program]))):
        result = pipeline.run_sync(output=RunOutputOptions(capture=False, echo=False))

    assert result.ok is True, "the pipeline should succeed"
    assert result.stdout is None, "capture=False must leave stdout unset"


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_flat_capture_echo_kwargs_are_deprecated() -> None:
    """The flat ``capture``/``echo`` kwargs still work but warn."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    echo = sh.make(ECHO)

    pipeline = echo("-n", "legacy") | python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )

    with (
        scoped(ScopeConfig(allowlist=frozenset([ECHO, python_program]))),
        pytest.warns(DeprecationWarning, match="RunOutputOptions"),
    ):
        result = pipeline.run_sync(capture=True, echo=False)

    assert result.ok is True, "the pipeline should succeed"
    assert result.stdout == "legacy", "the deprecated flags must still capture output"


def test_pipeline_rejects_output_combined_with_flat_kwargs() -> None:
    """Supplying both ``output`` and the deprecated flags raises ValueError."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    pipeline = python("-c", "print('a')") | python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        with pytest.raises(ValueError, match="not both"):
            pipeline.run_sync(output=RunOutputOptions(), capture=True)

        unknown_output_kwargs: dict[str, bool] = {"captuer": True}
        run_sync = typ.cast("cabc.Callable[..., object]", pipeline.run_sync)
        with pytest.raises(TypeError, match="unexpected keyword"):
            run_sync(**unknown_output_kwargs)


@pytest.mark.parametrize("invalid_bound", [0, -1])
def test_run_output_options_rejects_non_positive_echo_bound(
    invalid_bound: int,
) -> None:
    """A non-positive ``max_echo_line_bytes`` is rejected at construction."""
    with pytest.raises(ValueError, match="must be a positive integer"):
        RunOutputOptions(max_echo_line_bytes=invalid_bound)


def test_run_output_options_default_bound_matches_github_log_limit() -> None:
    """The default bound mirrors the GitHub Actions 64 KiB per-line limit."""
    assert RunOutputOptions().max_echo_line_bytes == DEFAULT_ECHO_MAX_LINE_BYTES
    assert DEFAULT_ECHO_MAX_LINE_BYTES == 64 * 1024


@pytest.mark.parametrize(
    ("capture", "expected_stdout", "expected_stderr"),
    [
        pytest.param(True, "INTERMEDIATE", "", id="capture-final-stdio"),
        pytest.param(False, None, None, id="discard-final-stdio"),
    ],
)
def test_pipeline_stdio_policy_streams_intermediate_stdout_end_to_end(
    *,
    capture: bool,
    expected_stdout: str | None,
    expected_stderr: str | None,
) -> None:
    """Pipeline execution streams intermediate stdout and applies final capture.

    Parameters
    ----------
    capture : bool
        Whether the final stage's stdio should be captured.
    expected_stdout : str | None
        The expected final stdout, or ``None`` when ``capture`` is ``False``
        and stdout is not captured.
    expected_stderr : str | None
        The expected final stderr, or ``None`` when ``capture`` is ``False``
        and stderr is not captured.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    producer = python("-c", "import sys; sys.stdout.write('intermediate')")
    transformer = python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read().upper())",
    )

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = (producer | transformer).run_sync(
            output=RunOutputOptions(capture=capture),
        )

    assert result.stdout == expected_stdout, (
        f"capture={capture}: result.stdout mismatch"
    )
    assert len(result.stages) == 2, f"capture={capture}: result.stages length mismatch"
    assert result.stages[0].stdout is None, (
        f"capture={capture}: stage 0 stdout mismatch"
    )
    assert result.stages[0].stderr == expected_stderr, (
        f"capture={capture}: stage 0 stderr mismatch"
    )
    assert result.stages[0].exit_code == 0, (
        f"capture={capture}: stage 0 exit_code mismatch"
    )
    assert result.stages[1].stdout == expected_stdout, (
        f"capture={capture}: stage 1 stdout mismatch"
    )
    assert result.stages[1].stderr == expected_stderr, (
        f"capture={capture}: stage 1 stderr mismatch"
    )
    assert result.stages[1].exit_code == 0, (
        f"capture={capture}: stage 1 exit_code mismatch"
    )
