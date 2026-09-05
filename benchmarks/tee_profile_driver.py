"""CLI and JSON output helpers for Cuprum tee hot-path profiling."""

from __future__ import annotations

import argparse
import json
import pathlib as pth
import random
import typing as typ

from benchmarks.tee_profile_configuration import (
    _config_from_args,
    _scenario_by_name,
)
from benchmarks.tee_profile_execution import (
    _build_profile_plan,
    _profile_dir,
    _ProfilePlan,
    _round_read_sizes,
    _run_profile_sweep,
)
from benchmarks.tee_profile_output import _write_json
from benchmarks.tee_profile_scenarios import (
    _DEFAULT_FIXTURE,
    _DEFAULT_OUTPUT_DIR,
    _DEFAULT_WRAPPED_FIXTURE,
    TeeProfileDriverConfig,
    TeeProfileScenario,
    default_tee_profile_scenarios,
)
from cuprum._streams_pump import _READ_SIZE

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def run_profile_plan(*, config: TeeProfileDriverConfig) -> _ProfilePlan:
    """Generate a serial, auditable profiling plan.

    Parameters
    ----------
    config:
        Driver configuration including fixture paths, output directory,
        profiler choice, and run counts.

    Returns
    -------
    dict[str, object]
        JSON-serializable plan with ``fixture_path``,
        ``wrapped_fixture_path``, ``output_dir``, ``profiler``,
        ``warmup_count``, ``repeat_count``, ``perf_frequency``,
        ``perf_call_graph``, and ``scenarios`` (list of dicts, each containing
        ``worker_command`` and ``profile_dir``).
    """
    scenario_matrices = tuple(
        default_tee_profile_scenarios(
            fixture_path=config.fixture_path,
            wrapped_fixture_path=config.wrapped_fixture_path,
            repeat_count=config.repeat_count,
            read_size=read_size,
        )
        for read_size in config.read_sizes
    )
    return _build_profile_plan(config=config, scenario_matrices=scenario_matrices)


def run_profile_scenario(
    *, config: TeeProfileDriverConfig
) -> cabc.Mapping[str, object]:
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
    scenario = _scenario_by_name(config)
    return _run_profile_scenario(
        scenario,
        config=config,
        scenario_dir=config.output_dir / scenario.name,
    )


def _run_profile_scenario(
    scenario: TeeProfileScenario,
    *,
    config: TeeProfileDriverConfig,
    scenario_dir: pth.Path,
) -> cabc.Mapping[str, object]:
    """Run one resolved scenario into its dedicated artefact directory."""
    from benchmarks.tee_profile_profilers import _profiler_for, _run_warmup

    scenario_dir.mkdir(parents=True, exist_ok=True)
    _write_json(scenario_dir / "scenario.json", scenario.as_dict())
    _run_warmup(scenario, warmup_count=config.warmup_count)
    return _profiler_for(config.profiler).run(
        scenario,
        scenario_dir=scenario_dir,
        config=config,
    )


def run_profile_sweep(
    *, config: TeeProfileDriverConfig
) -> list[cabc.Mapping[str, object]]:
    """Measure one named scenario across configured sizes and rounds."""
    return _run_profile_sweep(
        config=config,
        scenario_resolver=_scenario_by_name,
        scenario_runner=_run_profile_scenario,
        shuffle=_shuffle_for(config),
    )


def run_profile_matrix(
    *,
    config: TeeProfileDriverConfig,
) -> list[cabc.Mapping[str, object]]:
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
    results: list[cabc.Mapping[str, object]] = []
    shuffle = _shuffle_for(config)
    for round_index in range(1, config.rounds + 1):
        for read_size in _round_read_sizes(config, shuffle=shuffle):
            batch_results, failed = _run_matrix_batch(
                config=config,
                read_size=read_size,
                round_index=round_index,
            )
            results.extend(batch_results)
            if failed:
                return results
    return results


def _run_matrix_batch(
    *,
    config: TeeProfileDriverConfig,
    read_size: int,
    round_index: int,
) -> tuple[list[cabc.Mapping[str, object]], bool]:
    """Run one read-size matrix and report whether a scenario failed."""
    results: list[cabc.Mapping[str, object]] = []
    for scenario in default_tee_profile_scenarios(
        fixture_path=config.fixture_path,
        wrapped_fixture_path=config.wrapped_fixture_path,
        repeat_count=config.repeat_count,
        read_size=read_size,
    ):
        result = _run_profile_scenario(
            scenario,
            config=config,
            scenario_dir=_profile_dir(
                config,
                scenario,
                round_index=round_index,
            ),
        )
        results.append(result)
        if _worker_result_exit_status(result) != 0:
            return results, True
    return results, False


def _shuffle_for(
    config: TeeProfileDriverConfig,
) -> cabc.Callable[[list[int]], None] | None:
    """Construct the optional source of randomized read-size order."""
    return random.SystemRandom().shuffle if config.randomize_order else None


def _worker_result_exit_status(result: cabc.Mapping[str, object]) -> int:
    """Return the shell exit status implied by a worker result payload."""
    exit_code = result.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return exit_code
    if result.get("status") == "failed":
        return 1
    return 0


def _matrix_exit_status(results: cabc.Iterable[cabc.Mapping[str, object]]) -> int:
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
    parser.add_argument("--read-sizes", type=_parse_read_sizes, default=(_READ_SIZE,))
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--randomize-order", action="store_true")
    parser.add_argument("--perf-frequency", type=int, default=999)
    parser.add_argument("--perf-call-graph", default="dwarf,16384")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan")
    subparsers.add_parser("run")
    run_scenario = subparsers.add_parser("run-scenario")
    run_scenario.add_argument("--scenario", required=True)
    return parser


def _parse_read_sizes(value: str) -> tuple[int, ...]:
    """Parse a comma-separated read-size list for a profiling sweep."""
    try:
        read_sizes = tuple(int(item) for item in value.split(","))
    except ValueError as exc:
        msg = f"read-sizes must be comma-separated integers, got {value!r}"
        raise argparse.ArgumentTypeError(msg) from exc
    if not read_sizes or any(read_size < 1 for read_size in read_sizes):
        msg = "read-sizes must contain at least one positive integer"
        raise argparse.ArgumentTypeError(msg)
    return read_sizes


def main() -> int:
    """Run the tee profile driver CLI.

    Returns
    -------
    int
        Process exit code; 0 on success, non-zero on worker or configuration
        failure.

    Raises
    ------
    ValueError
        If the parsed subcommand is not recognized.
    """
    args = _base_parser().parse_args()
    config = _config_from_args(args)
    if args.command == "plan":
        print(json.dumps(run_profile_plan(config=config), indent=2, sort_keys=True))
        return 0
    if args.command == "run-scenario":
        if len(config.read_sizes) != 1 or config.rounds != 1:
            return _matrix_exit_status(run_profile_sweep(config=config))
        result = run_profile_scenario(config=config)
        return _worker_result_exit_status(result)
    if args.command == "run":
        results = run_profile_matrix(config=config)
        return _matrix_exit_status(results)
    msg = f"unknown command: {args.command}"
    raise ValueError(msg)
