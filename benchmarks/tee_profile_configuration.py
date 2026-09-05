"""Configuration resolution and worker-command construction for profiling."""

from __future__ import annotations

import sys
import typing as typ

from benchmarks.tee_profile_scenarios import (
    TeeProfileDriverConfig,
    TeeProfileScenario,
    default_tee_profile_scenarios,
)

if typ.TYPE_CHECKING:
    import argparse


def _resolved_read_size(
    config: TeeProfileDriverConfig,
    *,
    read_size: int | None,
) -> int:
    """Return an explicit read size or the sole configured value."""
    if read_size is not None:
        return read_size
    if len(config.read_sizes) != 1:
        msg = "one read size is required outside a sweep"
        raise ValueError(msg)
    return config.read_sizes[0]


def _named_scenario(
    scenarios: tuple[TeeProfileScenario, ...],
    *,
    scenario_name: str | None,
) -> TeeProfileScenario:
    """Return the named scenario from an ordered scenario matrix."""
    if scenario_name is None:
        msg = "scenario name is required"
        raise ValueError(msg)
    scenarios_by_name = {scenario.name: scenario for scenario in scenarios}
    try:
        return scenarios_by_name[scenario_name]
    except KeyError:
        valid = ", ".join(scenario.name for scenario in scenarios)
        msg = f"unknown scenario {scenario_name!r}; expected one of: {valid}"
        raise ValueError(msg) from None


def _scenario_by_name(
    config: TeeProfileDriverConfig,
    *,
    read_size: int | None = None,
) -> TeeProfileScenario:
    """Resolve one scenario from the configured matrix."""
    resolved_read_size = _resolved_read_size(config, read_size=read_size)
    scenarios = default_tee_profile_scenarios(
        fixture_path=config.fixture_path,
        wrapped_fixture_path=config.wrapped_fixture_path,
        repeat_count=config.repeat_count,
        read_size=resolved_read_size,
    )
    return _named_scenario(scenarios, scenario_name=config.scenario_name)


def _worker_command(scenario: TeeProfileScenario) -> list[str]:
    """Build an equivalent worker command for plans and manual reruns."""
    command = [
        sys.executable,
        "-m",
        "benchmarks.tee_profile_worker",
        "--fixture",
        str(scenario.fixture_path),
        "--stages",
        str(scenario.stages),
        "--mode",
        scenario.mode,
        "--sink-kind",
        scenario.sink_kind,
        "--backend",
        scenario.backend,
        "--repeat-count",
        str(scenario.repeat_count),
        "--read-size",
        str(scenario.read_size),
        "--encoding",
        scenario.encoding,
        "--errors",
        scenario.errors,
    ]
    if scenario.with_line_callbacks:
        command.append("--line-callbacks")
    return command


def _config_from_args(args: argparse.Namespace) -> TeeProfileDriverConfig:
    """Convert parsed arguments to driver configuration."""
    return TeeProfileDriverConfig(
        fixture_path=args.fixture,
        wrapped_fixture_path=args.wrapped_fixture,
        output_dir=args.output_dir,
        profiler=args.profiler,
        warmup_count=args.warmup_count,
        repeat_count=args.repeat_count,
        read_sizes=args.read_sizes,
        rounds=args.rounds,
        randomize_order=args.randomize_order,
        perf_frequency=args.perf_frequency,
        perf_call_graph=args.perf_call_graph,
        scenario_name=getattr(args, "scenario", None),
    )
