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
        echo_stdout=st.none() | st.booleans(),
        echo_stderr=st.none() | st.booleans(),
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


@given(
    capture=st.booleans(),
    echo=st.booleans(),
    echo_stdout=st.none() | st.booleans(),
    echo_stderr=st.none() | st.booleans(),
)
def test_run_output_options_resolves_per_stream_echo_from_shorthand(
    *,
    capture: bool,
    echo: bool,
    echo_stdout: bool | None,
    echo_stderr: bool | None,
) -> None:
    """Per-stream fields resolve to ``echo`` unless explicitly overridden.

    Resolution overwrites the per-stream fields, so the expected pair is
    rebuilt from the same inputs: a ``None`` field inherits ``echo``; an
    explicit field keeps its value.
    """
    options = RunOutputOptions(
        capture=capture,
        echo=echo,
        echo_stdout=echo_stdout,
        echo_stderr=echo_stderr,
    )

    assert options.echo_stdout is (echo if echo_stdout is None else echo_stdout), (
        "an explicit echo_stdout must take precedence over the echo shorthand"
    )
    assert options.echo_stderr is (echo if echo_stderr is None else echo_stderr), (
        "an explicit echo_stderr must take precedence over the echo shorthand"
    )


def test_run_output_options_echo_shorthand_resolves_both_streams() -> None:
    """Construction with only ``echo=True`` resolves both streams to ``True``."""
    options = RunOutputOptions(capture=True, echo=True)

    assert options.echo_stdout is True
    assert options.echo_stderr is True


def test_run_output_options_per_stream_override_takes_precedence() -> None:
    """An explicit per-stream override wins over the ``echo`` shorthand."""
    options = RunOutputOptions(capture=True, echo=True, echo_stdout=False)

    assert options.echo_stdout is False
    assert options.echo_stderr is True


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


@pytest.mark.usefixtures("stream_backend")
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_pipeline_per_stream_echo_is_independent(*, stream: str) -> None:
    """Pipeline echo of one stream leaves the other out of the parent."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    producer = python(
        "-c",
        "import sys; print('out'); print('err', file=sys.stderr)",
    )
    consumer = python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )
    echo_stdout = stream == "stdout"
    stdout_sink = io.StringIO()
    stderr_sink = io.StringIO()

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = (producer | consumer).run_sync(
            output=RunOutputOptions(
                capture=True,
                echo_stdout=echo_stdout,
                echo_stderr=not echo_stdout,
            ),
            context=ExecutionContext(stdout_sink=stdout_sink, stderr_sink=stderr_sink),
        )

    assert result.ok is True, "the pipeline should succeed"
    assert result.stdout == "out\n", "capture must return the final stage stdout"
    if echo_stdout:
        assert "out" in stdout_sink.getvalue(), (
            "stdout echo must follow echo_stdout=True"
        )
        assert stderr_sink.getvalue() == "", (
            "stderr must stay silent while only stdout echoes"
        )
    else:
        assert "err" in stderr_sink.getvalue(), (
            "stderr echo must follow echo_stderr=True"
        )
        assert stdout_sink.getvalue() == "", (
            "stdout must stay silent while only stderr echoes"
        )


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
