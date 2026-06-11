"""Shared benchmark dataclasses, TypedDicts, and validation helpers.

This module centralizes benchmark-facing data contracts used by throughput
command construction and dry-run payload generation. It defines immutable
dataclasses for scenario/config/result values and a TypedDict for serializable
scenario payloads.

Use these symbols when constructing benchmark plans in application code or
tests, and rely on their ``__post_init__`` methods for fail-fast validation of
core invariants (for example: non-empty names, positive payload sizes, and
valid iteration counts).

Examples
--------
>>> from benchmarks._benchmark_types import HyperfineConfig, PipelineBenchmarkScenario
>>> scenario = PipelineBenchmarkScenario(
...     name="pipeline-python",
...     backend="python",
...     payload_bytes=1024,
...     stages=3,
...     with_line_callbacks=False,
... )
>>> config = HyperfineConfig(warmup=1, runs=3)

Validation helpers are imported from
:mod:`benchmarks._benchmark_type_validators`; the dataclasses' ``__post_init__``
methods call those imported functions to enforce expected types and value
ranges.
"""

from __future__ import annotations

import dataclasses as dc

# ``pth`` is imported at runtime (not under ``TYPE_CHECKING``) so the public
# dataclass annotations (``output_path: pth.Path`` and similar) remain
# resolvable via ``typing.get_type_hints``; the TC003 suppression keeps ruff from
# pushing it back into a type-checking block.
import pathlib as pth  # noqa: TC003
import sys
import typing as typ

from benchmarks._benchmark_type_validators import (
    _validate_backend as _validate_backend_value,
)
from benchmarks._benchmark_type_validators import (
    _validate_bool,
    _validate_hyperfine_iterations,
    _validate_iteration_count,
    _validate_non_empty_string,
    _validate_path,
    _validate_payload_bytes,
    _validate_scenario_name,
    _validate_stages,
)

BackendName = typ.Literal["python", "rust"]


class PipelineBenchmarkScenarioDict(typ.TypedDict):
    """Represent the serialisable shape for a benchmark scenario.

    Examples
    --------
    >>> PipelineBenchmarkScenarioDict(
    ...     name="pipeline-python",
    ...     backend="python",
    ...     payload_bytes=1024,
    ...     stages=3,
    ...     with_line_callbacks=False,
    ... )
    {'name': 'pipeline-python', 'backend': 'python', 'payload_bytes': 1024, \
'stages': 3, 'with_line_callbacks': False}
    """

    name: str
    backend: BackendName
    payload_bytes: int
    stages: int
    with_line_callbacks: bool


@dc.dataclass(frozen=True, slots=True)
class PipelineBenchmarkScenario:
    """Configure one hyperfine benchmark scenario.

    Parameters
    ----------
    name:
        Human-readable scenario name used in benchmark output.
    backend:
        Stream backend to benchmark (`"python"` or `"rust"`).
    payload_bytes:
        Size of payload written into the benchmarked pipeline.
    stages:
        Number of pipeline stages executed by the worker.
    with_line_callbacks:
        Whether to enable line callback overhead in the worker.

    Raises
    ------
    ValueError
        If ``name`` is empty, ``payload_bytes`` is non-positive, ``stages`` is
        less than two, or ``backend`` is not supported.

    Examples
    --------
    >>> PipelineBenchmarkScenario(
    ...     name="pipeline-python",
    ...     backend="python",
    ...     payload_bytes=1024,
    ...     stages=3,
    ...     with_line_callbacks=False,
    ... )
    PipelineBenchmarkScenario(name='pipeline-python', backend='python', \
payload_bytes=1024, stages=3, with_line_callbacks=False)
    """

    name: str
    backend: BackendName
    payload_bytes: int
    stages: int
    with_line_callbacks: bool

    def __post_init__(self) -> None:
        """Validate scenario values that are critical for execution."""
        _validate_scenario_name(self.name)
        _validate_payload_bytes(self.payload_bytes)
        _validate_stages(self.stages)
        _validate_bool(self.with_line_callbacks, name="with_line_callbacks")
        _validate_backend(self.backend)

    def as_dict(self) -> PipelineBenchmarkScenarioDict:
        """Convert the scenario into a JSON-serialisable mapping.

        Returns
        -------
        PipelineBenchmarkScenarioDict
            Scenario fields ready for JSON encoding.

        Examples
        --------
        >>> PipelineBenchmarkScenario(
        ...     name="pipeline-python",
        ...     backend="python",
        ...     payload_bytes=1024,
        ...     stages=3,
        ...     with_line_callbacks=False,
        ... ).as_dict()
        {'name': 'pipeline-python', 'backend': 'python', 'payload_bytes': 1024, \
'stages': 3, 'with_line_callbacks': False}
        """
        return {
            "name": self.name,
            "backend": self.backend,
            "payload_bytes": self.payload_bytes,
            "stages": self.stages,
            "with_line_callbacks": self.with_line_callbacks,
        }


@dc.dataclass(frozen=True, slots=True)
class HyperfineConfig:
    """Configure hyperfine-specific iteration settings.

    Parameters
    ----------
    warmup:
        Number of warmup iterations per command.
    runs:
        Number of measured iterations per command.
    hyperfine_bin:
        Executable name or path used to invoke hyperfine.

    Raises
    ------
    TypeError
        If ``warmup`` or ``runs`` are not integers.
    ValueError
        If ``warmup`` is negative or ``runs`` is less than one.

    Examples
    --------
    >>> HyperfineConfig(warmup=1, runs=3)
    HyperfineConfig(warmup=1, runs=3, hyperfine_bin='hyperfine')
    """

    warmup: int
    runs: int
    hyperfine_bin: str = "hyperfine"

    def __post_init__(self) -> None:
        """Validate hyperfine invocation configuration."""
        _validate_non_empty_string(self.hyperfine_bin, name="hyperfine_bin")
        _validate_hyperfine_iterations(warmup=self.warmup, runs=self.runs)


def _validate_backend(value: object) -> BackendName:
    """Validate that a scenario backend is one of the supported values."""
    return typ.cast("BackendName", _validate_backend_value(value))


@dc.dataclass(frozen=True, slots=True)
class PipelineBenchmarkConfig:
    """Configure a full pipeline benchmark run.

    Parameters
    ----------
    output_path:
        Output path for hyperfine JSON or dry-run plan JSON.
    worker_path:
        Path to ``benchmarks/pipeline_worker.py``.
    scenarios:
        Benchmark scenarios that are converted into hyperfine commands.
    warmup:
        Warmup iteration count for hyperfine.
    runs:
        Measured iteration count for hyperfine.
    hyperfine_bin:
        Executable name or path for hyperfine.
    python_bin:
        Executable name or path for the Python interpreter used to run
        ``benchmarks/pipeline_worker.py``. Benchmark commands use the
        interpreter directly so ratchets do not measure ``uv run`` overhead.
    uv_bin:
        Deprecated compatibility setting. Scenario commands no longer use
        ``uv run`` during measured benchmark iterations.
    dry_run:
        Whether to emit plan JSON without invoking hyperfine.
    rust_available:
        Whether Rust backend support is available in the current environment.
    worker_iterations:
        Number of pipeline executions performed inside one worker process.

    Raises
    ------
    TypeError
        If boolean settings are not booleans.
    ValueError
        If configured executable names are empty.

    Examples
    --------
    >>> PipelineBenchmarkConfig(
    ...     output_path=pth.Path("dist/benchmarks/pipeline-throughput.json"),
    ...     worker_path=pth.Path("benchmarks/pipeline_worker.py"),
    ...     scenarios=(),
    ...     warmup=1,
    ...     runs=3,
    ... )
    PipelineBenchmarkConfig(...)
    """

    output_path: pth.Path
    worker_path: pth.Path
    scenarios: tuple[PipelineBenchmarkScenario, ...]
    warmup: int
    runs: int
    hyperfine_bin: str = "hyperfine"
    python_bin: str = sys.executable
    uv_bin: str | None = None
    dry_run: bool = False
    rust_available: bool = False
    worker_iterations: int = 20

    def __post_init__(self) -> None:
        """Validate benchmark configuration values."""
        object.__setattr__(
            self,
            "output_path",
            _validate_path(self.output_path, name="output_path"),
        )
        object.__setattr__(
            self,
            "worker_path",
            _validate_path(self.worker_path, name="worker_path"),
        )
        _validate_hyperfine_iterations(warmup=self.warmup, runs=self.runs)
        _validate_non_empty_string(self.hyperfine_bin, name="hyperfine_bin")
        _validate_non_empty_string(self.python_bin, name="python_bin")
        if self.uv_bin is not None:
            _validate_non_empty_string(self.uv_bin, name="uv_bin")
        _validate_bool(self.dry_run, name="dry_run")
        _validate_bool(self.rust_available, name="rust_available")
        _validate_iteration_count(
            self.worker_iterations,
            name="worker_iterations",
            min_value=1,
        )


@dc.dataclass(frozen=True, slots=True)
class PipelineBenchmarkRunResult:
    """Represent metadata produced by a benchmark run call.

    Parameters
    ----------
    dry_run:
        Whether execution was run in dry-run mode.
    command:
        Final command vector passed to hyperfine, or planned in dry-run mode.
    output_path:
        Path where benchmark JSON output or dry-run plan is written.
    rust_available:
        Rust extension availability recorded at execution time.
    scenarios:
        Scenario tuple used to build benchmark commands.

    Examples
    --------
    >>> PipelineBenchmarkRunResult(
    ...     dry_run=True,
    ...     command=("hyperfine",),
    ...     output_path=pth.Path("bench.json"),
    ...     rust_available=False,
    ...     scenarios=(),
    ... )
    PipelineBenchmarkRunResult(dry_run=True, command=('hyperfine',), \
output_path=PosixPath('bench.json'), rust_available=False, scenarios=())
    """

    dry_run: bool
    command: tuple[str, ...]
    output_path: pth.Path
    rust_available: bool
    scenarios: tuple[PipelineBenchmarkScenario, ...]


__all__ = [
    "HyperfineConfig",
    "PipelineBenchmarkConfig",
    "PipelineBenchmarkRunResult",
    "PipelineBenchmarkScenario",
    "PipelineBenchmarkScenarioDict",
]
