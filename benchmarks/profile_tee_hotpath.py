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

from benchmarks import tee_profile_scenarios as _scenarios
from benchmarks.tee_profile_driver import (
    _base_parser,
    _matrix_exit_status,
    _worker_result_exit_status,
    _write_json,
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
    _config_from_args,
    _line_callback_scenarios,
    _single_stage_no_callback_scenarios,
    _worker_command,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

_ORIGINAL_CAN_USE_RUST_BACKEND = _scenarios.can_use_rust_backend

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
    "_postprocess_perf",
    "_profiler_for",
    "_require_tool",
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
]


def can_use_rust_backend() -> bool:
    """Return whether Rust backend scenarios can run in this environment.

    Returns
    -------
    bool
        ``True`` when the Cuprum Rust extension is importable in the current
        environment; ``False`` otherwise.
    """
    return _ORIGINAL_CAN_USE_RUST_BACKEND()


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
    return (
        *_single_stage_no_callback_scenarios(fixture_path, repeat_count=repeat_count),
        *_line_callback_scenarios(wrapped_fixture_path, repeat_count=repeat_count),
        *_multi_stage_backend_scenarios(fixture_path, repeat_count=repeat_count),
    )


def _scenario_by_name(config: TeeProfileDriverConfig) -> TeeProfileScenario:
    """Resolve one scenario from the configured matrix."""
    scenarios = default_tee_profile_scenarios(
        fixture_path=config.fixture_path,
        wrapped_fixture_path=config.wrapped_fixture_path,
        repeat_count=config.repeat_count,
    )
    if config.scenario_name is None:
        msg = "scenario name is required"
        raise ValueError(msg)
    for scenario in scenarios:
        if scenario.name == config.scenario_name:
            return scenario
    valid = ", ".join(scenario.name for scenario in scenarios)
    msg = f"unknown scenario {config.scenario_name!r}; expected one of: {valid}"
    raise ValueError(msg)


class _PlanScenarioEntry(typ.TypedDict):
    """One scenario entry in a profiling plan."""

    name: str
    fixture_path: str
    stages: int
    mode: typ.Literal["capture", "echo", "tee"]
    sink_kind: typ.Literal["devnull", "pty_blackhole", "text_blackhole"]
    with_line_callbacks: bool
    backend: typ.Literal["auto", "python", "rust"]
    repeat_count: int
    encoding: str
    errors: str
    worker_command: list[str]
    profile_dir: str


class _ProfilePlan(typ.TypedDict):
    """Resolved profiling plan emitted by ``plan``."""

    fixture_path: str
    wrapped_fixture_path: str
    output_dir: str
    profiler: ProfilerName
    warmup_count: int
    repeat_count: int
    perf_frequency: int
    perf_call_graph: str
    scenarios: list[_PlanScenarioEntry]


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
            _PlanScenarioEntry(
                name=scenario.name,
                fixture_path=str(scenario.fixture_path),
                stages=scenario.stages,
                mode=scenario.mode,
                sink_kind=scenario.sink_kind,
                with_line_callbacks=scenario.with_line_callbacks,
                backend=scenario.backend,
                repeat_count=scenario.repeat_count,
                encoding=scenario.encoding,
                errors=scenario.errors,
                worker_command=_worker_command(scenario),
                profile_dir=str(config.output_dir / scenario.name),
            )
            for scenario in scenarios
        ],
    }


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
        results: list[cabc.Mapping[str, object]] = []
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

    return run_matrix()


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


if __name__ == "__main__":
    raise SystemExit(main())
