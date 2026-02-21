"""Behavioural tests for pipeline execution under explicit backend selection.

These BDD scenarios verify that pipelines produce correct results regardless
of which stream backend (Python or Rust) handles inter-stage data pumping.
"""

from __future__ import annotations

import typing as typ

import pytest
from pytest_bdd import given, parsers, scenario, then, when

from cuprum import ECHO, ScopeConfig, _pipeline_streams, _rust_backend, scoped, sh
from cuprum._backend import _check_rust_available, get_stream_backend
from tests.helpers.catalogue import combine_programs_into_catalogue, python_catalogue

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum.program import Program
    from cuprum.sh import PipelineResult


@pytest.fixture
def requires_rust_backend() -> None:
    """Skip the test at setup time when the Rust extension is unavailable."""
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed")


# -- Scenarios ----------------------------------------------------------------


@scenario(
    "../features/stream_backend_pipeline.feature",
    "Pipeline streams output using the Python backend",
)
def test_pipeline_python_backend() -> None:
    """Pipeline produces correct output with the Python backend."""


@pytest.mark.usefixtures("requires_rust_backend")
@scenario(
    "../features/stream_backend_pipeline.feature",
    "Pipeline streams output using the Rust backend",
)
def test_pipeline_rust_backend() -> None:
    """Pipeline produces correct output with the Rust backend."""


@scenario(
    "../features/stream_backend_pipeline.feature",
    "Pipeline streams output using the auto backend",
)
def test_pipeline_auto_backend() -> None:
    """Pipeline produces correct output with the auto backend."""


@scenario(
    "../features/stream_backend_pipeline.feature",
    "Pipeline falls back to Python pumping when Rust cannot use FDs",
)
def test_pipeline_forced_fallback() -> None:
    """Pipeline falls back to Python pump when Rust FD extraction fails."""


@scenario(
    "../features/stream_backend_pipeline.feature",
    "Pipeline raises when Rust is forced but unavailable",
)
def test_pipeline_forced_rust_unavailable_error() -> None:
    """Pipeline surfaces ImportError when Rust is forced but unavailable."""


# -- Given steps --------------------------------------------------------------


@given(
    parsers.parse("the stream backend is set to {backend}"),
    target_fixture="backend_env",
)
def given_stream_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> str:
    """Set the stream backend environment variable.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Pytest monkeypatch for environment variable isolation.
    backend : str
        The backend identifier to set.

    Returns
    -------
    str
        The backend identifier that was set.
    """
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", backend)
    return backend


@given("the Rust extension is reported as available")
def given_rust_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the Rust availability probe to return True."""
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()
    monkeypatch.setattr(_rust_backend, "is_available", lambda: True)


@given(
    "the Rust extension is not available for pipeline execution",
)
def given_rust_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the Rust availability probe to return False."""
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()
    monkeypatch.setattr(_rust_backend, "is_available", lambda: False)


@given(
    "inter-stage file descriptor extraction fails",
    target_fixture="python_pump_fallback_counter",
)
def given_fd_extraction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    """Force FD extraction to fail so dispatch falls back to Python pumping."""
    original_pump = _pipeline_streams._pump_stream
    call_counter = {"calls": 0}

    async def counted_pump(
        reader: asyncio.StreamReader | None,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        call_counter["calls"] += 1
        await original_pump(reader, writer)

    monkeypatch.setattr(_pipeline_streams, "_extract_reader_fd", lambda _: None)
    monkeypatch.setattr(_pipeline_streams, "_extract_writer_fd", lambda _: None)
    monkeypatch.setattr(_pipeline_streams, "_pump_stream", counted_pump)
    return call_counter


def _make_echo_python_pipeline(
    python_code: str,
) -> tuple[sh.Pipeline, frozenset[Program]]:
    """Build the shared echo-to-python pipeline for backend selection tests."""
    _, python_program = python_catalogue()
    catalogue = combine_programs_into_catalogue(
        ECHO,
        python_program,
        project_name="backend-pipeline-tests",
    )
    echo = sh.make(ECHO, catalogue=catalogue)
    python = sh.make(python_program, catalogue=catalogue)

    pipeline = echo("-n", "hello") | python(
        "-c",
        python_code,
    )
    allowlist = frozenset([ECHO, python_program])
    return pipeline, allowlist


@given(
    "a simple two stage uppercase pipeline",
    target_fixture="pipeline_under_test",
)
def given_uppercase_pipeline() -> tuple[sh.Pipeline, frozenset[Program]]:
    """Create a two stage pipeline that uppercases its input.

    Returns
    -------
    tuple[sh.Pipeline, frozenset[Program]]
        The pipeline and the allowlist of programs required.
    """
    return _make_echo_python_pipeline(
        "import sys; sys.stdout.write(sys.stdin.read().upper())",
    )


@given(
    "a pipeline with an immediately exiting downstream stage",
    target_fixture="pipeline_under_test",
)
def given_short_lived_downstream_pipeline() -> tuple[sh.Pipeline, frozenset[Program]]:
    """Create a pipeline whose downstream stage does not read stdin.

    Returns
    -------
    tuple[sh.Pipeline, frozenset[Program]]
        The pipeline and the allowlist of programs required.
    """
    return _make_echo_python_pipeline(
        "import sys; sys.stdout.write('OK')",
    )


# -- When steps ---------------------------------------------------------------


@when(
    "I run the pipeline synchronously",
    target_fixture="pipeline_result",
)
def when_run_sync(
    pipeline_under_test: tuple[sh.Pipeline, frozenset[Program]],
) -> PipelineResult:
    """Execute the pipeline via ``run_sync()``.

    Parameters
    ----------
    pipeline_under_test : tuple[sh.Pipeline, frozenset[Program]]
        The pipeline and its required allowlist.

    Returns
    -------
    PipelineResult
        The result of the synchronous pipeline execution.
    """
    pipeline, allowlist = pipeline_under_test
    with scoped(ScopeConfig(allowlist=allowlist)):
        return pipeline.run_sync()


@when(
    "I attempt to run the pipeline synchronously",
    target_fixture="pipeline_error",
)
def when_attempt_run_sync(
    pipeline_under_test: tuple[sh.Pipeline, frozenset[Program]],
) -> BaseException | None:
    """Execute ``run_sync()`` and capture any raised exception."""
    pipeline, allowlist = pipeline_under_test
    with scoped(ScopeConfig(allowlist=allowlist)):
        try:
            pipeline.run_sync()
        except ImportError as exc:
            return exc
    return None


# -- Then steps ---------------------------------------------------------------


@then("the pipeline stdout is the uppercased input")
def then_stdout_uppercased(pipeline_result: PipelineResult) -> None:
    """Assert that the pipeline produced uppercased output.

    Parameters
    ----------
    pipeline_result : PipelineResult
        The result from pipeline execution.
    """
    assert pipeline_result.stdout == "HELLO", (
        f"expected uppercased output 'HELLO', got {pipeline_result.stdout!r}"
    )


@then("all stages exit successfully")
def then_all_stages_ok(pipeline_result: PipelineResult) -> None:
    """Assert that every pipeline stage exited with code zero.

    Parameters
    ----------
    pipeline_result : PipelineResult
        The result from pipeline execution.
    """
    assert pipeline_result.ok is True, "pipeline result should indicate success"
    assert all(stage.exit_code == 0 for stage in pipeline_result.stages), (
        "every stage should exit with code 0"
    )


@then("the Python pump fallback is used")
def then_python_fallback_used(python_pump_fallback_counter: dict[str, int]) -> None:
    """Assert that the Python pump path was exercised."""
    assert python_pump_fallback_counter["calls"] > 0, (
        "expected Python pump fallback to be used at least once"
    )


@then("an ImportError is raised during pipeline execution")
def then_pipeline_import_error_raised(pipeline_error: BaseException | None) -> None:
    """Assert that forcing unavailable Rust raises ImportError."""
    assert isinstance(pipeline_error, ImportError), (
        f"expected ImportError, got {type(pipeline_error).__name__}: {pipeline_error}"
    )
    assert "CUPRUM_STREAM_BACKEND" in str(pipeline_error), (
        "expected ImportError to mention backend environment variable"
    )
