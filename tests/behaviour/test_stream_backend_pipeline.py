"""Behavioural tests for pipeline execution under explicit backend selection.

These BDD scenarios verify that pipelines produce correct results regardless
of which stream backend (Python or Rust) handles inter-stage data pumping.
"""

from __future__ import annotations

import typing as typ

import pytest
from pytest_bdd import given, parsers, scenario, then, when

from cuprum import ECHO, ScopeConfig, _rust_backend, scoped, sh
from cuprum._backend import _check_rust_available, get_stream_backend
from tests.helpers.catalogue import combine_programs_into_catalogue, python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.sh import PipelineResult


# -- Scenarios ----------------------------------------------------------------


@scenario(
    "../features/stream_backend_pipeline.feature",
    "Pipeline streams output using the Python backend",
)
def test_pipeline_python_backend() -> None:
    """Pipeline produces correct output with the Python backend."""


@pytest.mark.skipif(
    not _rust_backend.is_available(),
    reason="Rust extension is not installed",
)
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


@given(
    "a simple two stage uppercase pipeline",
    target_fixture="pipeline_under_test",
)
def given_uppercase_pipeline() -> tuple[sh.Pipeline, frozenset[typ.Any]]:
    """Create a two stage pipeline that uppercases its input.

    Returns
    -------
    tuple[Pipeline, frozenset]
        The pipeline and the allowlist of programs required.
    """
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
        "import sys; sys.stdout.write(sys.stdin.read().upper())",
    )
    allowlist = frozenset([ECHO, python_program])
    return pipeline, allowlist


# -- When steps ---------------------------------------------------------------


@when(
    "I run the pipeline synchronously",
    target_fixture="pipeline_result",
)
def when_run_sync(
    pipeline_under_test: tuple[sh.Pipeline, frozenset[typ.Any]],
) -> PipelineResult:
    """Execute the pipeline via ``run_sync()``.

    Parameters
    ----------
    pipeline_under_test : tuple[Pipeline, frozenset]
        The pipeline and its required allowlist.

    Returns
    -------
    PipelineResult
        The result of the synchronous pipeline execution.
    """
    pipeline, allowlist = pipeline_under_test
    with scoped(ScopeConfig(allowlist=allowlist)):
        return pipeline.run_sync()


# -- Then steps ---------------------------------------------------------------


@then("the pipeline stdout is the uppercased input")
def then_stdout_uppercased(pipeline_result: PipelineResult) -> None:
    """Assert that the pipeline produced uppercased output.

    Parameters
    ----------
    pipeline_result : PipelineResult
        The result from pipeline execution.
    """
    assert pipeline_result.stdout == "HELLO"


@then("all stages exit successfully")
def then_all_stages_ok(pipeline_result: PipelineResult) -> None:
    """Assert that every pipeline stage exited with code zero.

    Parameters
    ----------
    pipeline_result : PipelineResult
        The result from pipeline execution.
    """
    assert pipeline_result.ok is True
    assert all(stage.exit_code == 0 for stage in pipeline_result.stages)
