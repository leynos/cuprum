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

from benchmarks._benchmark_types import (
    HyperfineConfig,
    PipelineBenchmarkConfig,
    PipelineBenchmarkRunResult,
    PipelineBenchmarkScenario,
    PipelineBenchmarkScenarioDict,
)
from cuprum import is_rust_available

_SMOKE_PAYLOAD_BYTES = 1024
_DEFAULT_PAYLOAD_BYTES = 1024 * 1024

__all__ = [
    "HyperfineConfig",
    "PipelineBenchmarkConfig",
    "PipelineBenchmarkRunResult",
    "PipelineBenchmarkScenario",
    "PipelineBenchmarkScenarioDict",
    "build_hyperfine_command",
    "default_pipeline_scenarios",
    "main",
    "render_prefixed_command",
    "run_pipeline_benchmarks",
]


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


def _execute_hyperfine_benchmark(
    command: list[str],
    output_path: pth.Path,
) -> None:
    """Execute the hyperfine benchmark command.

    Parameters
    ----------
    command:
        Command list with hyperfine executable as first element.
    output_path:
        Path to write benchmark JSON output.

    Raises
    ------
    FileNotFoundError
        If hyperfine executable cannot be resolved.
    subprocess.CalledProcessError
        If hyperfine exits with a non-zero status.
    """
    command[0] = _resolve_executable(command[0])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603  # command built from fixed executable + controlled args
        command,
        check=True,
        timeout=1800,
    )


def _prepare_benchmark_command_config(
    *,
    config: PipelineBenchmarkConfig,
) -> PipelineBenchmarkConfig:
    """Prepare command rendering config, resolving the `uv` launcher when needed."""
    if config.dry_run:
        return config
    return dc.replace(config, uv_bin=_resolve_executable(config.uv_bin))


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
    command_config = _prepare_benchmark_command_config(config=config)
    command = build_hyperfine_command(config=command_config)

    if config.dry_run:
        _write_dry_run_payload(
            output_path=config.output_path,
            command=command,
            scenarios=config.scenarios,
            rust_available=config.rust_available,
        )
    else:
        _execute_hyperfine_benchmark(
            command=command,
            output_path=config.output_path,
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
