"""Shared benchmark dataclasses, TypedDicts, and validation helpers.

This module centralizes benchmark-facing data contracts used by throughput
command construction and dry-run payload generation. It defines immutable
dataclasses for scenario/config/result values and a TypedDict for serializable
scenario payloads.

Utility
-------
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

Validation helpers are defined alongside these dataclasses and are used by
their ``__post_init__`` methods to enforce expected types and value ranges.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import pathlib as pth

_VALID_BACKENDS = {"python", "rust"}
_MIN_PIPELINE_STAGES = 2

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


def _validate_int(value: object, *, name: str) -> int:
    """Validate integer values and reject booleans."""
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{name} must be an int, got {type(value).__name__}"
        raise TypeError(msg)
    return value


def _validate_bool(value: object, *, name: str) -> bool:
    """Validate boolean values."""
    if not isinstance(value, bool):
        msg = f"{name} must be a bool, got {type(value).__name__}"
        raise TypeError(msg)
    return value


def _validate_non_empty_string(value: object, *, name: str) -> str:
    """Validate non-empty string values."""
    if not isinstance(value, str) or not value.strip():
        msg = f"{name} must be a non-empty string"
        raise ValueError(msg)
    return value


def _validate_hyperfine_iterations(*, warmup: object, runs: object) -> None:
    """Validate hyperfine warmup and run counts."""
    validated_warmup = _validate_int(warmup, name="warmup")
    if validated_warmup < 0:
        msg = f"warmup must be >= 0, got {validated_warmup}"
        raise ValueError(msg)

    validated_runs = _validate_int(runs, name="runs")
    if validated_runs < 1:
        msg = f"runs must be >= 1, got {validated_runs}"
        raise ValueError(msg)


def _validate_scenario_name(value: object) -> str:
    """Validate that a scenario name is a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        msg = "name must be a non-empty string"
        raise ValueError(msg)
    return value


def _validate_payload_bytes(value: object) -> int:
    """Validate that scenario payload size is a positive integer."""
    payload_bytes = _validate_int(value, name="payload_bytes")
    if payload_bytes <= 0:
        msg = f"payload_bytes must be > 0, got {payload_bytes}"
        raise ValueError(msg)
    return payload_bytes


def _validate_stages(value: object) -> int:
    """Validate that scenario stage count meets the minimum threshold."""
    stages = _validate_int(value, name="stages")
    if stages < _MIN_PIPELINE_STAGES:
        msg = f"stages must be >= {_MIN_PIPELINE_STAGES}, got {stages}"
        raise ValueError(msg)
    return stages


def _validate_backend(value: BackendName) -> BackendName:
    """Validate that a scenario backend is one of the supported values."""
    if value not in _VALID_BACKENDS:
        msg = f"backend must be one of {sorted(_VALID_BACKENDS)}, got {value!r}"
        raise ValueError(msg)
    return value


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
    uv_bin:
        Executable name or path for the ``uv`` launcher.
    dry_run:
        Whether to emit plan JSON without invoking hyperfine.
    rust_available:
        Whether Rust backend support is available in the current environment.

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
    uv_bin: str = "uv"
    dry_run: bool = False
    rust_available: bool = False

    def __post_init__(self) -> None:
        """Validate benchmark configuration values."""
        _validate_hyperfine_iterations(warmup=self.warmup, runs=self.runs)
        _validate_non_empty_string(self.hyperfine_bin, name="hyperfine_bin")
        _validate_non_empty_string(self.uv_bin, name="uv_bin")
        _validate_bool(self.dry_run, name="dry_run")
        _validate_bool(self.rust_available, name="rust_available")


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
