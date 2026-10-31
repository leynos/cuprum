"""Unit tests for pipeline output option resolution."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import contextlib as cl
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


@pytest.mark.usefixtures("stream_backend")
@pytest.mark.parametrize(
    "deprecated_flat_flags",
    [
        pytest.param(False, id="output-options"),
        pytest.param(True, id="deprecated-flat-flags"),
    ],
)
def test_pipeline_echo_options_for_run_and_run_sync(
    pipeline_execution_strategy: tuple[str, PipelineExecuteFn],
    identity_pipeline: tuple[Pipeline, frozenset[Program]],
    deprecated_flat_flags: bool,
) -> None:
    """Pipeline entry points honour output options and deprecated flat flags."""
    _, execute = pipeline_execution_strategy
    pipeline, allowlist = identity_pipeline
    stdout_sink = io.StringIO()
    output_kwargs: dict[str, typ.Any] = (
        {"capture": True, "echo": True}
        if deprecated_flat_flags
        else {"output": RunOutputOptions(capture=True, echo=True)}
    )
    warning_context = (
        pytest.warns(DeprecationWarning, match="RunOutputOptions")
        if deprecated_flat_flags
        else cl.nullcontext()
    )

    with scoped(ScopeConfig(allowlist=allowlist)), warning_context:
        result = execute(
            pipeline,
            {
                **output_kwargs,
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
        max_echo_line_bytes=st.one_of(
            st.none(),
            st.integers(min_value=1, max_value=1 << 20),
        ),
        group=st.booleans(),
        annotate_failure=st.booleans(),
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
        assert resolved.capture is flags.get("capture", True), (
            "legacy capture flags must resolve to the supplied value"
        )
        assert resolved.echo is flags.get("echo", False), (
            "legacy echo flags must resolve to the supplied value"
        )
        return

    resolved = _resolve_pipeline_output(output, flags)
    if output is None:
        # The default options are reconstructible byte-for-byte, so equality is
        # meaningful here.
        assert resolved == RunOutputOptions(), (
            "omitted flags must resolve to the default options"
        )
        return
    # Not an equality check: the convenience flags may have synthesized a sink,
    # and two distinct adapters are not equal, so the supplied object is
    # compared by identity and by the fields resolution promises to carry.
    assert resolved is output, (
        "omitted flags must resolve to the supplied options object"
    )
    assert resolved.capture == output.capture, (
        "resolution must preserve the capture option"
    )
    assert resolved.max_echo_line_bytes == output.max_echo_line_bytes, (
        "resolution must preserve the maximum echoed-line size"
    )
    assert (resolved.group, resolved.annotate_failure) == (
        output.group,
        output.annotate_failure,
    ), "resolution must carry the convenience flags unchanged"


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

    assert options.echo_stdout is True, "echo=True must enable stdout echoing"
    assert options.echo_stderr is True, "echo=True must enable stderr echoing"


def test_run_output_options_per_stream_override_takes_precedence() -> None:
    """An explicit per-stream override wins over the ``echo`` shorthand."""
    options = RunOutputOptions(capture=True, echo=True, echo_stdout=False)

    assert options.echo_stdout is False
    assert options.echo_stderr is True


_REPEATED_LINE = "x" * 80 + "\n"
_REPEATED_LINE_COUNT = 3
_REPEATED_BOUND = 50
_REPEATED_ECHO = "x" * 25 + "… [truncated 55 bytes]\n"


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_bounds_each_repeated_echo_line(
    pipeline_execution_strategy: tuple[str, PipelineExecuteFn],
) -> None:
    """Every repeated over-limit pipeline line carries its own marker."""
    _, execute = pipeline_execution_strategy
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    payload = _REPEATED_LINE * _REPEATED_LINE_COUNT
    producer = python(
        "-c",
        f"import sys; sys.stdout.buffer.write({payload.encode('utf-8')!r})",
    )
    # Binary passthrough keeps the producer's LF bytes intact on Windows,
    # where text-mode stdout would translate every LF to CRLF.
    pipeline = producer | python(
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    )
    sink = io.StringIO()

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = execute(
            pipeline,
            {
                "output": RunOutputOptions(
                    capture=True,
                    echo=True,
                    max_echo_line_bytes=_REPEATED_BOUND,
                ),
                "context": ExecutionContext(stdout_sink=sink),
            },
        )

    assert result.ok is True, "the pipeline should succeed"
    assert result.stdout == payload, "capture must retain every repeated line"
    assert sink.getvalue() == _REPEATED_ECHO * _REPEATED_LINE_COUNT, (
        f"each echoed line must be bounded exactly, got={sink.getvalue()!r}"
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
