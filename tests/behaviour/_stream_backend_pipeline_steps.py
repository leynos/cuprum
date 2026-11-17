"""Given/When step definitions for the stream backend pipeline scenarios.

``test_stream_backend_pipeline.py`` declares the scenarios and owns the ``Then``
steps, which carry the assertions; ``assert`` statements are permitted only in
``test_*.py`` files. That module registers this one through ``pytest_plugins``,
so every step defined here is reachable from a collected test, and the split
keeps both files inside the 400-line limit.

Each step body is a single call or a short orchestration, so a step is a name
for the work rather than a second place for it.
"""

from __future__ import annotations

import time
import typing as typ

import pytest
from pytest_bdd import given, parsers, when

from cuprum import ScopeConfig, scoped
from cuprum._backend import _check_rust_available, get_stream_backend
from cuprum._testing import (
    force_python_pump_fallback,
    set_rust_availability_for_testing,
)
from tests.behaviour._native_pipeline_hand_off import _make_echo_python_pipeline
from tests.helpers.catalogue import early_close_pipeline

if typ.TYPE_CHECKING:
    from cuprum import sh
    from cuprum.program import Program
    from cuprum.sh import PipelineResult

# The deadline the released-producer scenario allows. A released stage settles
# in well under a second, so half of this bound still fails a fallback that
# waits for the producer instead of releasing its reader.
_EARLY_CLOSE_DEADLINE_S = 10.0


class _PipelineRun(typ.NamedTuple):
    """A timed pipeline run, reported with the deadline it was given.

    The deadline travels with the result so an assertion can judge the elapsed
    time without importing this module's constant into the test module, which
    would pre-import the step plugin.
    """

    result: PipelineResult
    elapsed_s: float
    deadline_s: float


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
def given_rust_available() -> None:
    """Force the Rust availability probe to return True."""
    set_rust_availability_for_testing(is_available=True)


@given(
    "the Rust extension is not available for pipeline execution",
)
def given_rust_unavailable() -> None:
    """Force the Rust availability probe to return False."""
    set_rust_availability_for_testing(is_available=False)


@given(
    "inter-stage file descriptor extraction fails",
    target_fixture="python_pump_fallback_counter",
)
def given_fd_extraction_fails() -> dict[str, int]:
    """Force FD extraction to fail so dispatch falls back to Python pumping.

    Returns
    -------
    dict[str, int]
        Mutable call counter updated when the Python fallback pump executes.
    """
    return force_python_pump_fallback()


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
    "a continuing producer piped into an early-exiting consumer",
    target_fixture="pipeline_under_test",
)
def given_continuing_producer_pipeline() -> tuple[sh.Pipeline, frozenset[Program]]:
    """Create the released-producer regression pipeline.

    Returns
    -------
    tuple[sh.Pipeline, frozenset[Program]]
        The pipeline and the allowlist of programs required.
    """
    return early_close_pipeline()


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
    "I run the pipeline synchronously with a deadline",
    target_fixture="pipeline_run",
)
def when_run_sync_with_deadline(
    pipeline_under_test: tuple[sh.Pipeline, frozenset[Program]],
) -> _PipelineRun:
    """Execute the pipeline under the release deadline and time the run.

    Parameters
    ----------
    pipeline_under_test : tuple[sh.Pipeline, frozenset[Program]]
        The pipeline and the allowlist of programs required.

    Returns
    -------
    _PipelineRun
        The result of the synchronous run, the seconds it took, and the
        deadline it was given.
    """
    pipeline, allowlist = pipeline_under_test
    started = time.monotonic()
    with scoped(ScopeConfig(allowlist=allowlist)):
        result = pipeline.run_sync(timeout=_EARLY_CLOSE_DEADLINE_S)
    return _PipelineRun(result, time.monotonic() - started, _EARLY_CLOSE_DEADLINE_S)


@when(
    "I attempt to run the pipeline synchronously",
    target_fixture="pipeline_error",
)
def when_attempt_run_sync(
    pipeline_under_test: tuple[sh.Pipeline, frozenset[Program]],
) -> ImportError:
    """Execute ``run_sync()`` and capture the forced-rust import failure.

    Parameters
    ----------
    pipeline_under_test : tuple[sh.Pipeline, frozenset[Program]]
        The pipeline and its required allowlist.

    Returns
    -------
    ImportError
        The ImportError raised when forced Rust is unavailable.
    """
    pipeline, allowlist = pipeline_under_test
    with (
        scoped(ScopeConfig(allowlist=allowlist)),
        pytest.raises(ImportError, match="CUPRUM_STREAM_BACKEND") as exc_info,
    ):
        pipeline.run_sync()
    return exc_info.value
