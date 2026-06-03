"""Command construction and execution for pipeline throughput benchmarks."""

from __future__ import annotations

import dataclasses as dc
import json
import os
import shutil
import subprocess  # noqa: S404  # benchmark runner intentionally invokes external tooling
import typing as typ

from benchmarks._benchmark_types import (
    HyperfineConfig,
    PipelineBenchmarkConfig,
    PipelineBenchmarkRunResult,
    PipelineBenchmarkScenario,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth


def render_prefixed_command(
    *,
    command: cabc.Sequence[str],
    env: cabc.Mapping[str, str],
) -> str:
    """Render a shell command with deterministic environment prefixes."""
    if os.name == "nt":
        return _render_windows_command(command=command, env=env)
    return _render_posix_command(command=command, env=env)


def _render_posix_command(
    *,
    command: cabc.Sequence[str],
    env: cabc.Mapping[str, str],
) -> str:
    """Render a POSIX shell command with environment prefixes."""
    env_tokens = [
        f"{key}={_quote_posix_shell_token(value)}" for key, value in sorted(env.items())
    ]
    command_text = " ".join(_quote_posix_shell_token(token) for token in command)
    if not env_tokens:
        return command_text
    return f"{' '.join(env_tokens)} {command_text}"


def _render_windows_command(
    *,
    command: cabc.Sequence[str],
    env: cabc.Mapping[str, str],
) -> str:
    """Render a cmd.exe command with environment prefixes."""
    command_text = " ".join(_quote_windows_cmd_token(token) for token in command)
    if not env:
        return command_text
    env_tokens = [
        f'set "{key}={_escape_windows_env_value(value)}"'
        for key, value in sorted(env.items())
    ]
    return f"{' && '.join(env_tokens)} && {command_text}"


def _quote_posix_shell_token(token: str) -> str:
    """Quote one POSIX shell token without relying on platform shell helpers."""
    safe_chars = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@%_+=:,./-",
    )
    if token and all(char in safe_chars for char in token):
        return token
    return "'" + token.replace("'", "'\"'\"'") + "'"


def _escape_windows_env_value(value: str) -> str:
    """Escape an environment value for cmd.exe ``set "KEY=value"`` syntax."""
    return value.replace('"', '""')


def _quote_windows_cmd_token(token: str) -> str:
    """Quote one token for a cmd.exe command string."""
    safe_chars = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:\\/-",
    )
    if token and all(char in safe_chars for char in token):
        return token

    quoted = ['"']
    backslashes = 0
    for char in token:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            quoted.extend(("\\" * ((backslashes * 2) + 1), '"'))
        else:
            quoted.extend(("\\" * backslashes, char))
        backslashes = 0
    quoted.extend(("\\" * (backslashes * 2), '"'))
    return "".join(quoted)


def _build_worker_command(
    *,
    scenario: PipelineBenchmarkScenario,
    worker_path: pth.Path,
    python_bin: str,
) -> list[str]:
    """Build the worker invocation for one benchmark scenario."""
    command = [
        python_bin,
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
    """Construct a hyperfine command vector for configured scenarios."""
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
            python_bin=config.python_bin,
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
    command: cabc.Sequence[str],
    scenarios: cabc.Sequence[PipelineBenchmarkScenario],
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
    """Execute the hyperfine benchmark command."""
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
    """Prepare command rendering config, resolving executables when needed."""
    if config.dry_run:
        return config
    return dc.replace(config, python_bin=_resolve_executable(config.python_bin))


def run_pipeline_benchmarks(
    *, config: PipelineBenchmarkConfig
) -> PipelineBenchmarkRunResult:
    """Execute pipeline benchmarks or write a dry-run benchmark plan."""
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
