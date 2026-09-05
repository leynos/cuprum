"""Cuprum tee hot-path profiling driver.

This module is the public driver and compatibility surface for the Cuprum tee
profiling harness. Use it when measuring parent-side final stream consumption
for ``echo=True`` and ``capture=True`` workloads, including sink write cost,
line-callback overhead, capture accumulation, and the boundary between
inter-stage pumping and final stream consumption.

Run the harness with ``python -m benchmarks.profile_tee_hotpath``. The
``plan`` subcommand emits an auditable JSON scenario plan, while
``run-scenario`` and ``run`` write per-scenario directories containing
``scenario.json``, ``worker-result.json``, and, when a profiler is enabled,
profiler artefacts such as ``perf.data``, ``perf.report.txt``,
``stacks.folded``, and ``summary.json``.

Example: ``python -m benchmarks.profile_tee_hotpath --profiler none run``.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

from benchmarks.tee_profile_configuration import (
    _config_from_args,
    _worker_command,
)
from benchmarks.tee_profile_driver import (
    _base_parser,
    _matrix_exit_status,
    _worker_result_exit_status,
    _write_json,
)
from benchmarks.tee_profile_execution import (
    _build_profile_plan,
    _ProfilePlan,
    _run_profile_sweep,
)
from benchmarks.tee_profile_profilers import (
    ProfilerAdapter,
    _NoneProfiler,
    _PerfProfiler,
    _postprocess_perf,
    _profiler_for,
    _PySpyProfiler,
    _require_tool,
    _run_perf,
    _run_py_spy,
    _run_warmup,
    _run_worker_measured,
)
from benchmarks.tee_profile_scenarios import (
    ProfilerName,
    TeeProfileDriverConfig,
    TeeProfileScenario,
    _line_callback_scenarios,
    _single_stage_no_callback_scenarios,
    can_use_rust_backend,
)
from cuprum._streams_pump import _READ_SIZE

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

__all__ = [
    "ProfilerAdapter",
    "ProfilerName",
    "TeeProfileDriverConfig",
    "TeeProfileScenario",
    "_NoneProfiler",
    "_PerfProfiler",
    "_PySpyProfiler",
    "_base_parser",
    "_config_from_args",
    "_line_callback_scenarios",
    "_matrix_exit_status",
    "_multi_stage_backend_scenarios",
    "_named_scenario",
    "_postprocess_perf",
    "_profiler_for",
    "_require_tool",
    "_resolved_read_size",
    "_run_perf",
    "_run_py_spy",
    "_run_warmup",
    "_run_worker_measured",
    "_scenario_by_name",
    "_single_stage_no_callback_scenarios",
    "_worker_command",
    "_worker_result_exit_status",
    "_write_json",
    "can_use_rust_backend",
    "default_tee_profile_scenarios",
    "main",
    "run_profile_matrix",
    "run_profile_plan",
    "run_profile_scenario",
    "run_profile_sweep",
]


def _multi_stage_backend_scenarios(
    fixture_path: pth.Path,
    *,
    repeat_count: int,
) -> tuple[TeeProfileScenario, ...]:
    """Return multi-stage scenarios for Python/Rust backend comparison."""
    python_scenario = TeeProfileScenario(
        name="echo-devnull-nocb-s4-python",
        fixture_path=fixture_path,
        stages=4,
        mode="echo",
        sink_kind="devnull",
        with_line_callbacks=False,
        backend="python",
        repeat_count=repeat_count,
    )
    if not can_use_rust_backend():
        return (python_scenario,)
    return (
        python_scenario,
        TeeProfileScenario(
            name="echo-devnull-nocb-s4-rust",
            fixture_path=fixture_path,
            stages=4,
            mode="echo",
            sink_kind="devnull",
            with_line_callbacks=False,
            backend="rust",
            repeat_count=repeat_count,
        ),
    )


def default_tee_profile_scenarios(
    *,
    fixture_path: pth.Path,
    wrapped_fixture_path: pth.Path,
    repeat_count: int,
    read_size: int = _READ_SIZE,
) -> tuple[TeeProfileScenario, ...]:
    """Return the required initial tee profiling scenario matrix.

    Parameters
    ----------
    fixture_path:
        Path to the unwrapped base64 fixture used by most scenarios.
    wrapped_fixture_path:
        Path to the wrap-76 fixture used by line-callback scenarios.
    repeat_count:
        Measured repeat count applied to every scenario.

    Returns
    -------
    tuple[TeeProfileScenario, ...]
        Ordered tuple of default profiling scenarios. The Rust-backend scenario
        omitted when ``can_use_rust_backend()`` returns ``False``.
    """
    scenarios = (
        *_single_stage_no_callback_scenarios(fixture_path, repeat_count=repeat_count),
        *_line_callback_scenarios(wrapped_fixture_path, repeat_count=repeat_count),
        *_multi_stage_backend_scenarios(fixture_path, repeat_count=repeat_count),
    )
    return tuple(dc.replace(scenario, read_size=read_size) for scenario in scenarios)


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


def run_profile_plan(*, config: TeeProfileDriverConfig) -> _ProfilePlan:
    """Generate a serial, auditable profiling plan.

    Parameters
    ----------
    config:
        Driver configuration including fixture paths, output directory,
        profiler choice, and run counts.

    Returns
    -------
    _ProfilePlan
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


def _run_profile_scenario(
    scenario: TeeProfileScenario,
    *,
    config: TeeProfileDriverConfig,
    scenario_dir: pth.Path,
) -> cabc.Mapping[str, object]:
    """Run one resolved scenario into its dedicated artefact directory."""
    scenario_dir.mkdir(parents=True, exist_ok=True)
    _write_json(scenario_dir / "scenario.json", scenario.as_dict())
    _run_warmup(scenario, warmup_count=config.warmup_count)
    return _profiler_for(config.profiler).run(
        scenario,
        scenario_dir=scenario_dir,
        config=config,
    )


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


def run_profile_sweep(
    *, config: TeeProfileDriverConfig
) -> list[cabc.Mapping[str, object]]:
    """Measure one named scenario across configured sizes and rounds."""
    return _run_profile_sweep(
        config=config,
        scenario_resolver=_scenario_by_name,
        scenario_runner=_run_profile_scenario,
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

    def run_matrix() -> list[cabc.Mapping[str, object]]:
        if len(config.read_sizes) != 1 or config.rounds != 1:
            msg = "read-size sweeps require the run-scenario command"
            raise ValueError(msg)
        results: list[cabc.Mapping[str, object]] = []
        for scenario in default_tee_profile_scenarios(
            fixture_path=config.fixture_path,
            wrapped_fixture_path=config.wrapped_fixture_path,
            repeat_count=config.repeat_count,
            read_size=config.read_sizes[0],
        ):
            scenario_config = dc.replace(config, scenario_name=scenario.name)
            result = run_profile_scenario(config=scenario_config)
            results.append(result)
            if _worker_result_exit_status(result) != 0:
                break
        return results

    return run_matrix()


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
        If the parsed CLI command is not one of ``plan``, ``run-scenario``, or
        ``run``.
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


if __name__ == "__main__":
    raise SystemExit(main())
