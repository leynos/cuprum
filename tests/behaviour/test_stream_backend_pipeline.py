"""Behavioural tests for pipeline execution under explicit backend selection.

These BDD scenarios verify that pipelines produce correct results regardless
of which stream backend (Python or Rust) handles inter-stage data pumping, and
that a fallback hop releases an upstream producer whose consumer exits early.
The ``Given`` and ``When`` steps live in
:mod:`tests.behaviour._stream_backend_pipeline_steps`; the ``Then`` steps stay
here because only ``test_*.py`` files may use ``assert``.
"""

from __future__ import annotations

import sys
import typing as typ

import pytest
from pytest_bdd import scenario, then

from cuprum import _rust_backend, sh
from cuprum._backend import StreamBackend, _check_rust_available, get_stream_backend
from cuprum._testing import (
    reset_pump_stream_dispatch_for_testing,
    set_rust_availability_for_testing,
)
from tests.behaviour._native_pipeline_hand_off import (
    _make_echo_python_pipeline,
    assert_deep_native_pipeline_completes,
    assert_repeated_native_pipeline_hand_off,
)
from tests.helpers.catalogue import (
    cat_program,
    combine_programs_into_catalogue,
    python_catalogue,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import PipelineResult
    from tests.behaviour._stream_backend_pipeline_steps import _PipelineRun

pytest_plugins = ("tests.behaviour._stream_backend_pipeline_steps",)


@pytest.fixture
def requires_rust_backend() -> None:
    """Skip the test at setup time when the Rust extension is unavailable."""
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed")


@pytest.fixture(autouse=True)
def reset_stream_dispatch_test_hooks() -> cabc.Iterator[None]:
    """Reset stream-dispatch test hooks before and after each scenario."""
    set_rust_availability_for_testing(is_available=None)
    reset_pump_stream_dispatch_for_testing()
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()
    yield
    set_rust_availability_for_testing(is_available=None)
    reset_pump_stream_dispatch_for_testing()
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()


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


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux raw-FD hand-off")
@pytest.mark.usefixtures("requires_rust_backend")
def test_auto_backend_repeated_native_pipeline_hand_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTO native pumping settles repeated real two-stage pipelines."""
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "auto")
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()

    active_backend = get_stream_backend()
    assert active_backend is StreamBackend.RUST, (
        "the Linux hand-off regression requires AUTO to resolve to Rust"
    )
    assert_repeated_native_pipeline_hand_off(
        active_backend,
        _make_echo_python_pipeline,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux raw-FD hand-off")
@pytest.mark.usefixtures("requires_rust_backend")
def test_auto_backend_deep_native_pipeline_hand_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline deeper than the idle worker pool still pumps a large payload."""
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "auto")
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()

    active_backend = get_stream_backend()
    assert active_backend is StreamBackend.RUST, (
        "the deep hand-off regression requires AUTO to resolve to Rust"
    )
    _, python_program = python_catalogue()
    cat = cat_program()
    catalogue = combine_programs_into_catalogue(
        python_program,
        cat,
        project_name="deep-native-pipeline-tests",
    )
    assert_deep_native_pipeline_completes(
        active_backend,
        frozenset([python_program, cat]),
        python_builder=sh.make(python_program, catalogue=catalogue),
        cat_builder=sh.make(cat, catalogue=catalogue),
    )


@scenario(
    "../features/stream_backend_pipeline.feature",
    "Pipeline falls back to Python pumping when Rust cannot use FDs",
)
def test_pipeline_forced_fallback() -> None:
    """Pipeline falls back to Python pump when Rust FD extraction fails."""


@scenario(
    "../features/stream_backend_pipeline.feature",
    "A continuing producer settles when the Rust pump falls back",
)
def test_pipeline_fallback_releases_continuing_producer() -> None:
    """Python fallback pumping settles a producer that never stops writing."""


@scenario(
    "../features/stream_backend_pipeline.feature",
    "Pipeline raises when Rust is forced but unavailable",
)
def test_pipeline_forced_rust_unavailable_error() -> None:
    """Pipeline surfaces ImportError when Rust is forced but unavailable."""


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


@then("the pipeline settles within its deadline")
def then_pipeline_settles_within_deadline(pipeline_run: _PipelineRun) -> None:
    """Assert the pipeline settled well inside its configured timeout.

    Parameters
    ----------
    pipeline_run : _PipelineRun
        The timed run under the release deadline.
    """
    assert pipeline_run.elapsed_s < pipeline_run.deadline_s / 2, (
        "a released producer must not consume the pipeline deadline: "
        f"took {pipeline_run.elapsed_s:.2f}s of {pipeline_run.deadline_s}s"
    )


@then("the early consumer reports completion and exits successfully")
def then_early_consumer_settles(pipeline_run: _PipelineRun) -> None:
    """Assert the early consumer kept its own output and a clean exit status.

    Parameters
    ----------
    pipeline_run : _PipelineRun
        The timed run under the release deadline.
    """
    assert pipeline_run.result.stdout == "done", (
        "the early consumer should report completion, got "
        f"{pipeline_run.result.stdout!r}"
    )
    assert pipeline_run.result.stages[1].exit_code == 0, (
        "the early consumer should exit cleanly, got "
        f"{pipeline_run.result.stages[1].exit_code}"
    )


@then("the Python pump fallback is used")
def then_python_fallback_used(python_pump_fallback_counter: dict[str, int]) -> None:
    """Assert that the Python pump path was exercised.

    Parameters
    ----------
    python_pump_fallback_counter : dict[str, int]
        Counter dictionary populated by the fallback setup step.
    """
    assert python_pump_fallback_counter["calls"] > 0, (
        "expected Python pump fallback to be used at least once"
    )


@then("an ImportError is raised during pipeline execution")
def then_pipeline_import_error_raised(pipeline_error: ImportError) -> None:
    """Assert that forcing unavailable Rust raises ImportError.

    Parameters
    ----------
    pipeline_error : ImportError
        ImportError captured during attempted pipeline execution.
    """
    assert "CUPRUM_STREAM_BACKEND" in str(pipeline_error), (
        "expected ImportError to mention backend environment variable"
    )
