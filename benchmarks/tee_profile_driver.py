"""CLI and JSON output helpers for Cuprum tee hot-path profiling."""

from __future__ import annotations

import argparse
import dataclasses as dc
import json
import pathlib as pth
import typing as typ

from benchmarks.tee_profile_scenarios import (
    _DEFAULT_FIXTURE,
    _DEFAULT_OUTPUT_DIR,
    _DEFAULT_WRAPPED_FIXTURE,
    TeeProfileDriverConfig,
    _config_from_args,
    _scenario_by_name,
    _worker_command,
    default_tee_profile_scenarios,
)


def run_profile_plan(*, config: TeeProfileDriverConfig) -> dict[str, object]:
    """Generate a serial, auditable profiling plan.

    Parameters
    ----------
    config:
        Driver configuration including fixture paths, output directory,
        profiler choice, and run counts.

    Returns
    -------
    dict[str, object]
        JSON-serialisable plan with ``fixture_path``,
        ``wrapped_fixture_path``, ``output_dir``, ``profiler``,
        ``warmup_count``, ``repeat_count``, ``perf_frequency``,
        ``perf_call_graph``, and ``scenarios`` (list of dicts, each containing
        ``worker_command`` and ``profile_dir``).
    """
    scenarios = default_tee_profile_scenarios(
        fixture_path=config.fixture_path,
        wrapped_fixture_path=config.wrapped_fixture_path,
        repeat_count=config.repeat_count,
    )
    return {
        "fixture_path": str(config.fixture_path),
        "wrapped_fixture_path": str(config.wrapped_fixture_path),
        "output_dir": str(config.output_dir),
        "profiler": config.profiler,
        "warmup_count": config.warmup_count,
        "repeat_count": config.repeat_count,
        "perf_frequency": config.perf_frequency,
        "perf_call_graph": config.perf_call_graph,
        "scenarios": [
            {
                **scenario.as_dict(),
                "worker_command": _worker_command(scenario),
                "profile_dir": str(config.output_dir / scenario.name),
            }
            for scenario in scenarios
        ],
    }


def _write_json(path: pth.Path, payload: typ.Mapping[str, object]) -> None:
    """Write stable JSON output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def run_profile_scenario(*, config: TeeProfileDriverConfig) -> typ.Mapping[str, object]:
    """Run one scenario, optionally under a profiler.

    Parameters
    ----------
    config:
        Driver configuration with ``scenario_name`` identifying the scenario
        to execute.

    Returns
    -------
    Mapping[str, object]
        Worker result mapping as produced by ``run_tee_profile_worker``.
    """
    from benchmarks.tee_profile_profilers import _profiler_for, _run_warmup

    scenario = _scenario_by_name(config)
    scenario_dir = config.output_dir / scenario.name
    scenario_dir.mkdir(parents=True, exist_ok=True)
    _write_json(scenario_dir / "scenario.json", scenario.as_dict())
    _run_warmup(scenario, warmup_count=config.warmup_count)
    return _profiler_for(config.profiler).run(
        scenario,
        scenario_dir=scenario_dir,
        config=config,
    )


def run_profile_matrix(
    *,
    config: TeeProfileDriverConfig,
) -> list[typ.Mapping[str, object]]:
    """Run all scenarios serially in the fixed matrix order.

    Parameters
    ----------
    config:
        Driver configuration for the full scenario matrix.

    Returns
    -------
    list[Mapping[str, object]]
        List of worker result mappings in default scenario matrix order.
        Execution stops and propagates the failure result on the first
        non-zero exit code.
    """
    results: list[typ.Mapping[str, object]] = []
    for scenario in default_tee_profile_scenarios(
        fixture_path=config.fixture_path,
        wrapped_fixture_path=config.wrapped_fixture_path,
        repeat_count=config.repeat_count,
    ):
        scenario_config = dc.replace(config, scenario_name=scenario.name)
        result = run_profile_scenario(config=scenario_config)
        results.append(result)
        if _worker_result_exit_status(result) != 0:
            break
    return results


def _worker_result_exit_status(result: typ.Mapping[str, object]) -> int:
    """Return the shell exit status implied by a worker result payload."""
    exit_code = result.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return exit_code
    if result.get("status") == "failed":
        return 1
    return 0


def _matrix_exit_status(results: typ.Iterable[typ.Mapping[str, object]]) -> int:
    """Return the first failing shell status from a scenario result sequence."""
    for result in results:
        exit_status = _worker_result_exit_status(result)
        if exit_status != 0:
            return exit_status
    return 0


def _base_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=pth.Path,
        default=_DEFAULT_FIXTURE,
        help="Unwrapped base64 fixture path.",
    )
    parser.add_argument(
        "--wrapped-fixture",
        type=pth.Path,
        default=_DEFAULT_WRAPPED_FIXTURE,
        help="Wrap-76 base64 fixture path for line callback scenarios.",
    )
    parser.add_argument(
        "--output-dir",
        type=pth.Path,
        default=_DEFAULT_OUTPUT_DIR,
    )
    parser.add_argument(
        "--profiler", choices=("none", "perf", "py-spy"), default="none"
    )
    parser.add_argument("--warmup-count", type=int, default=1)
    parser.add_argument("--repeat-count", type=int, default=3)
    parser.add_argument("--perf-frequency", type=int, default=999)
    parser.add_argument("--perf-call-graph", default="dwarf,16384")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan")
    subparsers.add_parser("run")
    run_scenario = subparsers.add_parser("run-scenario")
    run_scenario.add_argument("--scenario", required=True)
    return parser


def main() -> int:
    """Run the tee profile driver CLI.

    Returns
    -------
    int
        Process exit code; 0 on success, non-zero on worker or configuration
        failure.
    """
    args = _base_parser().parse_args()
    config = _config_from_args(args)
    if args.command == "plan":
        print(json.dumps(run_profile_plan(config=config), indent=2, sort_keys=True))
        return 0
    if args.command == "run-scenario":
        result = run_profile_scenario(config=config)
        return _worker_result_exit_status(result)
    if args.command == "run":
        results = run_profile_matrix(config=config)
        return _matrix_exit_status(results)
    msg = f"unknown command: {args.command}"
    raise ValueError(msg)
