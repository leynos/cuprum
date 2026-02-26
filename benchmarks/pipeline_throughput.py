"""Run end-to-end pipeline throughput benchmarks with hyperfine."""

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

BackendName = typ.Literal["python", "rust"]


class PipelineBenchmarkScenarioDict(typ.TypedDict):
    """JSON-serialisable shape for benchmark scenarios."""

    name: str
    backend: BackendName
    payload_bytes: int
    stages: int
    with_line_callbacks: bool


@dc.dataclass(frozen=True, slots=True)
class PipelineBenchmarkScenario:
    """Configuration for one hyperfine command scenario."""

    name: str
    backend: BackendName
    payload_bytes: int
    stages: int
    with_line_callbacks: bool

    def __post_init__(self) -> None:
        """Validate scenario values that are critical for execution."""
        if self.backend not in _VALID_BACKENDS:
            msg = (
                f"backend must be one of {sorted(_VALID_BACKENDS)}, "
                f"got {self.backend!r}"
            )
            raise ValueError(msg)

    def as_dict(self) -> PipelineBenchmarkScenarioDict:
        """Return a JSON-serialisable representation."""
        return {
            "name": self.name,
            "backend": self.backend,
            "payload_bytes": self.payload_bytes,
            "stages": self.stages,
            "with_line_callbacks": self.with_line_callbacks,
        }


@dc.dataclass(frozen=True, slots=True)
class HyperfineConfig:
    """Configuration for hyperfine invocation parameters."""

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
    """Configuration for running pipeline benchmarks."""

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
    """Result metadata for a benchmark CLI invocation."""

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
    """Build the default scenario matrix for throughput benchmarks."""
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
    """Render an env-prefixed shell command for hyperfine."""
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
    """Construct the hyperfine command from benchmark scenarios."""
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
    """Run hyperfine benchmarks or emit a dry-run plan JSON."""
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
    """Entry point for the pipeline throughput benchmark CLI."""
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
