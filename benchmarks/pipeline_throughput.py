"""Benchmark end-to-end pipeline throughput with hyperfine.

This module defines benchmark scenarios, command rendering helpers, and a CLI
for running or dry-running pipeline throughput measurements.

Utility
-------
The runner can:

- construct a scenario matrix for Python and optional Rust backends;
- render environment-prefixed worker commands for hyperfine; and
- execute hyperfine or emit a dry-run JSON plan for CI validation.

Usage
-----
Use the CLI directly or call the helpers from tests and automation.

Examples
--------
Run a smoke dry-run plan:

>>> # doctest: +SKIP
>>> main_args = [
...     "python",
...     "benchmarks/pipeline_throughput.py",
...     "--smoke",
...     "--dry-run",
...     "--output",
...     "dist/benchmarks/pipeline-throughput-plan.json",
... ]
"""

from __future__ import annotations

import argparse
import dataclasses as dc
import json
import pathlib as pth
import shlex
import shutil
import subprocess  # noqa: S404  # benchmark runner intentionally invokes external tooling
import typing as typ

from cuprum import is_rust_available

_SMOKE_PAYLOAD_BYTES = 1024
_DEFAULT_PAYLOAD_BYTES = 1024 * 1024
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
        if not isinstance(self.name, str) or not self.name.strip():
            msg = "name must be a non-empty string"
            raise ValueError(msg)

        payload_bytes = _validate_int(self.payload_bytes, name="payload_bytes")
        if payload_bytes <= 0:
            msg = f"payload_bytes must be > 0, got {payload_bytes}"
            raise ValueError(msg)

        stages = _validate_int(self.stages, name="stages")
        if stages < _MIN_PIPELINE_STAGES:
            msg = f"stages must be >= {_MIN_PIPELINE_STAGES}, got {stages}"
            raise ValueError(msg)

        if self.backend not in _VALID_BACKENDS:
            msg = (
                f"backend must be one of {sorted(_VALID_BACKENDS)}, "
                f"got {self.backend!r}"
            )
            raise ValueError(msg)

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


def default_pipeline_scenarios(
    *,
    smoke: bool,
    include_rust: bool,
) -> tuple[PipelineBenchmarkScenario, ...]:
    """Build the default benchmark scenario matrix.

    Parameters
    ----------
    smoke:
        When ``True``, use reduced payload size for quick validation runs.
    include_rust:
        When ``True``, include a Rust backend scenario.

    Returns
    -------
    tuple[PipelineBenchmarkScenario, ...]
        Ordered scenario tuple with a Python baseline and optional Rust case.

    Examples
    --------
    >>> scenarios = default_pipeline_scenarios(smoke=True, include_rust=False)
    >>> [scenario.backend for scenario in scenarios]
    ['python']
    """
    payload_bytes = _SMOKE_PAYLOAD_BYTES if smoke else _DEFAULT_PAYLOAD_BYTES
    scenarios: list[PipelineBenchmarkScenario] = [
        PipelineBenchmarkScenario(
            name="pipeline-python",
            backend="python",
            payload_bytes=payload_bytes,
            stages=3,
            with_line_callbacks=False,
        ),
    ]
    if include_rust:
        scenarios.append(
            PipelineBenchmarkScenario(
                name="pipeline-rust",
                backend="rust",
                payload_bytes=payload_bytes,
                stages=3,
                with_line_callbacks=False,
            ),
        )
    return tuple(scenarios)


def render_prefixed_command(
    *,
    command: typ.Sequence[str],
    env: typ.Mapping[str, str],
) -> str:
    """Render a shell command with deterministic environment prefixes.

    Parameters
    ----------
    command:
        Tokenised command to render with shell quoting.
    env:
        Environment variables prefixed in sorted key order.

    Returns
    -------
    str
        Rendered command string suitable for hyperfine command arguments.

    Examples
    --------
    >>> render_prefixed_command(command=["echo", "hello world"], env={"A": "1"})
    "A=1 echo 'hello world'"
    """
    env_tokens = [f"{key}={shlex.quote(value)}" for key, value in sorted(env.items())]
    command_text = shlex.join(list(command))
    if not env_tokens:
        return command_text
    return f"{' '.join(env_tokens)} {command_text}"


def _build_worker_command(
    *,
    scenario: PipelineBenchmarkScenario,
    worker_path: pth.Path,
    uv_bin: str,
) -> list[str]:
    """Build the worker invocation for one benchmark scenario."""
    command = [
        uv_bin,
        "run",
        "python",
        str(worker_path),
        "--payload-bytes",
        str(scenario.payload_bytes),
        "--stages",
        str(scenario.stages),
    ]
    if scenario.with_line_callbacks:
        command.append("--line-callbacks")
    return command


def _resolve_executable(name: str) -> str:
    """Resolve an executable to an absolute path."""
    resolved = shutil.which(name)
    if resolved is None:
        msg = f"required executable is not on PATH: {name}"
        raise FileNotFoundError(msg)
    return resolved


def build_hyperfine_command(*, config: PipelineBenchmarkConfig) -> list[str]:
    """Construct a hyperfine command vector for configured scenarios.

    Parameters
    ----------
    config:
        Pipeline benchmark configuration used to build command arguments.

    Returns
    -------
    list[str]
        Hyperfine command vector with one rendered worker command per scenario.

    Raises
    ------
    ValueError
        If no scenarios are configured.

    Examples
    --------
    >>> cfg = PipelineBenchmarkConfig(
    ...     output_path=pth.Path("bench.json"),
    ...     worker_path=pth.Path("benchmarks/pipeline_worker.py"),
    ...     scenarios=(
    ...         PipelineBenchmarkScenario(
    ...             name="pipeline-python",
    ...             backend="python",
    ...             payload_bytes=1024,
    ...             stages=3,
    ...             with_line_callbacks=False,
    ...         ),
    ...     ),
    ...     warmup=1,
    ...     runs=3,
    ... )
    >>> build_hyperfine_command(config=cfg)[0]
    'hyperfine'
    """
    hyperfine_config = HyperfineConfig(
        warmup=config.warmup,
        runs=config.runs,
        hyperfine_bin=config.hyperfine_bin,
    )
    if not config.scenarios:
        msg = "at least one benchmark scenario is required"
        raise ValueError(msg)

    command = [
        hyperfine_config.hyperfine_bin,
        "--export-json",
        str(config.output_path),
        "--warmup",
        str(hyperfine_config.warmup),
        "--runs",
        str(hyperfine_config.runs),
    ]
    for scenario in config.scenarios:
        worker_command = _build_worker_command(
            scenario=scenario,
            worker_path=config.worker_path,
            uv_bin=config.uv_bin,
        )
        command.append(
            render_prefixed_command(
                command=worker_command,
                env={"CUPRUM_STREAM_BACKEND": scenario.backend},
            ),
        )
    return command


def _write_dry_run_payload(
    *,
    output_path: pth.Path,
    command: typ.Sequence[str],
    scenarios: typ.Sequence[PipelineBenchmarkScenario],
    rust_available: bool,
) -> None:
    """Write dry-run benchmark metadata to JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dry_run": True,
        "rust_available": rust_available,
        "command": list(command),
        "scenarios": [scenario.as_dict() for scenario in scenarios],
    }
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def run_pipeline_benchmarks(
    *, config: PipelineBenchmarkConfig
) -> PipelineBenchmarkRunResult:
    """Execute pipeline benchmarks or write a dry-run benchmark plan.

    Parameters
    ----------
    config:
        Full benchmark configuration controlling command rendering and
        execution mode.

    Returns
    -------
    PipelineBenchmarkRunResult
        Metadata describing the benchmark command, output location, and
        scenario set.

    Raises
    ------
    FileNotFoundError
        If required executables cannot be resolved on ``PATH`` in non-dry-run
        mode.
    subprocess.CalledProcessError
        If hyperfine exits with a non-zero status.

    Examples
    --------
    >>> cfg = PipelineBenchmarkConfig(
    ...     output_path=pth.Path("bench.json"),
    ...     worker_path=pth.Path("benchmarks/pipeline_worker.py"),
    ...     scenarios=(
    ...         PipelineBenchmarkScenario(
    ...             name="pipeline-python",
    ...             backend="python",
    ...             payload_bytes=1024,
    ...             stages=3,
    ...             with_line_callbacks=False,
    ...         ),
    ...     ),
    ...     warmup=1,
    ...     runs=2,
    ...     dry_run=True,
    ... )
    >>> run_pipeline_benchmarks(config=cfg).dry_run
    True
    """
    if config.dry_run:
        command_config = config
    else:
        command_config = dc.replace(
            config,
            uv_bin=_resolve_executable(config.uv_bin),
        )

    command = build_hyperfine_command(config=command_config)

    if config.dry_run:
        _write_dry_run_payload(
            output_path=config.output_path,
            command=command,
            scenarios=config.scenarios,
            rust_available=config.rust_available,
        )
    else:
        command[0] = _resolve_executable(command[0])
        config.output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603  # command built from fixed executable + controlled args
            command,
            check=True,
            timeout=1800,
        )

    return PipelineBenchmarkRunResult(
        dry_run=config.dry_run,
        command=tuple(command),
        output_path=config.output_path,
        rust_available=config.rust_available,
        scenarios=config.scenarios,
    )


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the throughput runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=pth.Path,
        required=True,
        help="Path for hyperfine JSON output (or dry-run plan output).",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Use a 1 KB payload and fewer iterations for fast validation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write scenario/command plan JSON without invoking hyperfine.",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Number of warmup runs for each hyperfine command.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Number of measured runs for each hyperfine command.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the benchmark CLI entry point.

    Returns
    -------
    int
        Process exit code, where ``0`` indicates success.

    Raises
    ------
    FileNotFoundError
        If required executables are missing in non-dry-run mode.
    subprocess.CalledProcessError
        If benchmark execution fails.

    Examples
    --------
    >>> # doctest: +SKIP
    >>> raise SystemExit(main())
    """
    args = _parse_args()
    rust_available = is_rust_available()
    scenarios = default_pipeline_scenarios(
        smoke=args.smoke,
        include_rust=rust_available,
    )
    worker_path = pth.Path(__file__).with_name("pipeline_worker.py")

    config = PipelineBenchmarkConfig(
        output_path=args.output,
        worker_path=worker_path,
        scenarios=scenarios,
        warmup=args.warmup,
        runs=args.runs,
        dry_run=args.dry_run,
        rust_available=rust_available,
    )
    run_pipeline_benchmarks(config=config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
