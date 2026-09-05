"""Planning and sweep execution for tee profiling measurements."""

from __future__ import annotations

import typing as typ

from benchmarks.tee_profile_configuration import _worker_command
from benchmarks.tee_profile_output import _write_json

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

    from benchmarks.tee_profile_scenarios import (
        ProfilerName,
        TeeProfileDriverConfig,
        TeeProfileScenario,
    )


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
    read_size: int
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
    read_sizes: tuple[int, ...]
    rounds: int
    randomize_order: bool
    perf_frequency: int
    perf_call_graph: str
    scenarios: list[_PlanScenarioEntry]


def _profile_dir(
    config: TeeProfileDriverConfig,
    scenario: TeeProfileScenario,
    *,
    round_index: int,
) -> pth.Path:
    """Return the artefact path matching a planned measurement."""
    if len(config.read_sizes) == 1 and config.rounds == 1:
        return config.output_dir / scenario.name
    return (
        config.output_dir
        / scenario.name
        / f"read-size-{scenario.read_size}"
        / f"round-{round_index:02d}"
    )


def _build_profile_plan(
    *,
    config: TeeProfileDriverConfig,
    scenario_matrices: tuple[tuple[TeeProfileScenario, ...], ...],
) -> _ProfilePlan:
    """Build an auditable plan for every configured measurement."""
    scenarios: list[_PlanScenarioEntry] = []
    for round_index in range(1, config.rounds + 1):
        for matrix in scenario_matrices:
            scenarios.extend(
                _PlanScenarioEntry(
                    name=scenario.name,
                    fixture_path=str(scenario.fixture_path),
                    stages=scenario.stages,
                    mode=scenario.mode,
                    sink_kind=scenario.sink_kind,
                    with_line_callbacks=scenario.with_line_callbacks,
                    backend=scenario.backend,
                    repeat_count=scenario.repeat_count,
                    read_size=scenario.read_size,
                    encoding=scenario.encoding,
                    errors=scenario.errors,
                    worker_command=_worker_command(scenario),
                    profile_dir=str(
                        _profile_dir(config, scenario, round_index=round_index)
                    ),
                )
                for scenario in matrix
            )
    return _ProfilePlan(
        fixture_path=str(config.fixture_path),
        wrapped_fixture_path=str(config.wrapped_fixture_path),
        output_dir=str(config.output_dir),
        profiler=config.profiler,
        warmup_count=config.warmup_count,
        repeat_count=config.repeat_count,
        read_sizes=config.read_sizes,
        rounds=config.rounds,
        randomize_order=config.randomize_order,
        perf_frequency=config.perf_frequency,
        perf_call_graph=config.perf_call_graph,
        scenarios=scenarios,
    )


def _run_profile_sweep(
    *,
    config: TeeProfileDriverConfig,
    scenario_resolver: cabc.Callable[..., TeeProfileScenario],
    scenario_runner: cabc.Callable[..., cabc.Mapping[str, object]],
    shuffle: cabc.Callable[[list[int]], None] | None,
) -> list[cabc.Mapping[str, object]]:
    """Measure one named scenario across configured sizes and rounds."""
    if config.scenario_name is None:
        msg = "scenario name is required for a read-size sweep"
        raise ValueError(msg)
    samples: list[cabc.Mapping[str, object]] = []
    for round_index in range(config.rounds):
        for read_size in _round_read_sizes(config, shuffle=shuffle):
            scenario = scenario_resolver(config, read_size=read_size)
            sample_dir = _profile_dir(config, scenario, round_index=round_index + 1)
            samples.append(
                scenario_runner(
                    scenario,
                    config=config,
                    scenario_dir=sample_dir,
                )
            )
    _write_json(
        config.output_dir / config.scenario_name / "read-size-sweep.json",
        {
            "randomize_order": config.randomize_order,
            "read_sizes": list(config.read_sizes),
            "rounds": config.rounds,
            "samples": samples,
        },
    )
    return samples


def _round_read_sizes(
    config: TeeProfileDriverConfig,
    *,
    shuffle: cabc.Callable[[list[int]], None] | None,
) -> list[int]:
    """Return the read sizes for one round, applying injected randomization."""
    read_sizes = list(config.read_sizes)
    if not config.randomize_order:
        return read_sizes
    if shuffle is None:
        msg = "a shuffle dependency is required when randomize-order is enabled"
        raise ValueError(msg)
    shuffle(read_sizes)
    return read_sizes
