"""Scenario driver for Cuprum tee hot-path profiling."""

from __future__ import annotations

import argparse
import dataclasses as dc
import json
import os
import pathlib as pth
import shutil
import subprocess  # noqa: S404  # profiling driver intentionally invokes tools
import sys
import time
import typing as typ

from benchmarks.summarize_folded import summarize_folded_file
from benchmarks.tee_profile_worker import (
    TeeProfileWorkerConfig,
    TeeProfileWorkerResult,
    run_tee_profile_worker,
)
from cuprum import is_rust_available

if typ.TYPE_CHECKING:
    from benchmarks.sinks import SinkKind
    from benchmarks.tee_profile_worker import BackendName, TeeMode

type ProfilerName = typ.Literal["none", "perf", "py-spy"]

_DEFAULT_OUTPUT_DIR = pth.Path("dist/profiles")
_DEFAULT_FIXTURE = pth.Path("dist/fixtures/seed12345-nowrap.b64")
_DEFAULT_WRAPPED_FIXTURE = pth.Path("dist/fixtures/seed12345-wrap76.b64")


def can_use_rust_backend() -> bool:
    """Return whether Rust backend scenarios can run in this environment."""
    return is_rust_available()


@dc.dataclass(frozen=True, slots=True)
class TeeProfileScenario:
    """One resolved tee profiling scenario."""

    name: str
    fixture_path: pth.Path
    stages: int
    mode: TeeMode
    sink_kind: SinkKind
    with_line_callbacks: bool
    backend: BackendName
    repeat_count: int
    encoding: str = "utf-8"
    errors: str = "replace"

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable scenario mapping."""
        return {
            "name": self.name,
            "fixture_path": str(self.fixture_path),
            "stages": self.stages,
            "mode": self.mode,
            "sink_kind": self.sink_kind,
            "with_line_callbacks": self.with_line_callbacks,
            "backend": self.backend,
            "repeat_count": self.repeat_count,
            "encoding": self.encoding,
            "errors": self.errors,
        }

    def worker_config(
        self, *, repeat_count: int | None = None
    ) -> TeeProfileWorkerConfig:
        """Convert this scenario into a worker configuration."""
        return TeeProfileWorkerConfig(
            fixture_path=self.fixture_path,
            stages=self.stages,
            mode=self.mode,
            sink_kind=self.sink_kind,
            with_line_callbacks=self.with_line_callbacks,
            backend=self.backend,
            repeat_count=self.repeat_count if repeat_count is None else repeat_count,
            encoding=self.encoding,
            errors=self.errors,
        )


@dc.dataclass(frozen=True, slots=True)
class TeeProfileDriverConfig:
    """Configuration for scenario planning and execution."""

    fixture_path: pth.Path = _DEFAULT_FIXTURE
    wrapped_fixture_path: pth.Path = _DEFAULT_WRAPPED_FIXTURE
    output_dir: pth.Path = _DEFAULT_OUTPUT_DIR
    profiler: ProfilerName = "none"
    warmup_count: int = 1
    repeat_count: int = 3
    perf_frequency: int = 999
    perf_call_graph: str = "dwarf,16384"
    scenario_name: str | None = None

    def __post_init__(self) -> None:
        """Validate driver configuration."""
        int_bounds: tuple[tuple[str, int, int], ...] = (
            ("warmup-count", self.warmup_count, 0),
            ("repeat-count", self.repeat_count, 1),
            ("perf-frequency", self.perf_frequency, 1),
        )
        for name, value, minimum in int_bounds:
            if value < minimum:
                msg = f"{name} must be >= {minimum}, got {value}"
                raise ValueError(msg)
        if not self.perf_call_graph.strip():
            msg = "perf-call-graph must be a non-empty string"
            raise ValueError(msg)


def _single_stage_no_callback_scenarios(
    fixture_path: pth.Path,
    *,
    repeat_count: int,
) -> tuple[TeeProfileScenario, ...]:
    """Return single-stage, no-callback scenarios across sinks and modes."""
    return (
        TeeProfileScenario(
            name="echo-devnull-nocb-s1",
            fixture_path=fixture_path,
            stages=1,
            mode="echo",
            sink_kind="devnull",
            with_line_callbacks=False,
            backend="auto",
            repeat_count=repeat_count,
        ),
        TeeProfileScenario(
            name="echo-textblackhole-nocb-s1",
            fixture_path=fixture_path,
            stages=1,
            mode="echo",
            sink_kind="text_blackhole",
            with_line_callbacks=False,
            backend="auto",
            repeat_count=repeat_count,
        ),
        TeeProfileScenario(
            name="echo-pty-nocb-s1",
            fixture_path=fixture_path,
            stages=1,
            mode="echo",
            sink_kind="pty_blackhole",
            with_line_callbacks=False,
            backend="auto",
            repeat_count=repeat_count,
        ),
        TeeProfileScenario(
            name="tee-devnull-nocb-s1",
            fixture_path=fixture_path,
            stages=1,
            mode="tee",
            sink_kind="devnull",
            with_line_callbacks=False,
            backend="auto",
            repeat_count=repeat_count,
        ),
    )


def _line_callback_scenarios(
    wrapped_fixture_path: pth.Path,
    *,
    repeat_count: int,
) -> tuple[TeeProfileScenario, ...]:
    """Return single-stage line-callback scenarios using the wrapped fixture."""
    return (
        TeeProfileScenario(
            name="echo-devnull-cb-s1",
            fixture_path=wrapped_fixture_path,
            stages=1,
            mode="echo",
            sink_kind="devnull",
            with_line_callbacks=True,
            backend="auto",
            repeat_count=repeat_count,
        ),
    )


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
    """Return the required initial tee profiling scenario matrix."""
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


def _worker_command(scenario: TeeProfileScenario) -> list[str]:
    """Build an equivalent worker command for plans and manual reruns."""
    command = [
        sys.executable,
        str(pth.Path(__file__).with_name("tee_profile_worker.py")),
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
        "--encoding",
        scenario.encoding,
        "--errors",
        scenario.errors,
    ]
    if scenario.with_line_callbacks:
        command.append("--line-callbacks")
    return command


def run_profile_plan(*, config: TeeProfileDriverConfig) -> dict[str, object]:
    """Generate a serial, auditable profiling plan."""
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


def _run_warmup(scenario: TeeProfileScenario, *, warmup_count: int) -> None:
    """Run warm-up executions without profiler sampling."""
    if warmup_count == 0:
        return
    warmup_config = scenario.worker_config(repeat_count=warmup_count)
    result = run_tee_profile_worker(warmup_config)
    if result["exit_code"] != 0:
        msg = f"warm-up failed for {scenario.name}: {result}"
        raise RuntimeError(msg)


def _run_worker_measured(
    scenario: TeeProfileScenario,
    *,
    scenario_dir: pth.Path,
) -> TeeProfileWorkerResult:
    """Run the worker directly and write ``worker-result.json``."""
    result = run_tee_profile_worker(scenario.worker_config())
    _write_json(scenario_dir / "worker-result.json", result)
    return result


def _require_tool(name: str) -> str:
    """Resolve a profiler executable."""
    resolved = shutil.which(name)
    if resolved is None:
        msg = f"required profiler executable is not on PATH: {name}"
        raise FileNotFoundError(msg)
    return resolved


def _run_perf(
    scenario: TeeProfileScenario,
    *,
    scenario_dir: pth.Path,
    config: TeeProfileDriverConfig,
) -> dict[str, object]:
    """Record one scenario with Linux ``perf`` and generate text artefacts."""
    perf = _require_tool("perf")
    perf_data = scenario_dir / "perf.data"
    worker_result = scenario_dir / "worker-result.json"
    command = [
        perf,
        "record",
        "-F",
        str(config.perf_frequency),
        "-g",
        "--call-graph",
        config.perf_call_graph,
        "-o",
        str(perf_data),
        "--",
        *_worker_command(scenario),
        "--output",
        str(worker_result),
    ]
    started = time.perf_counter()
    env = os.environ.copy()
    env.setdefault("PYTHONPERFSUPPORT", "1")
    completed = subprocess.run(command, check=False, env=env)  # noqa: S603
    wall_time = time.perf_counter() - started
    if completed.returncode != 0:
        msg = f"perf record failed for {scenario.name} with {completed.returncode}"
        raise RuntimeError(msg)
    result = json.loads(worker_result.read_text())
    result["profile_wall_time_seconds"] = wall_time
    _write_json(worker_result, result)
    _postprocess_perf(scenario_dir=scenario_dir)
    return typ.cast("dict[str, object]", result)


def _postprocess_perf(*, scenario_dir: pth.Path) -> None:
    """Create text call tree, folded stacks, and summary artefacts."""
    perf = _require_tool("perf")
    report = subprocess.run(  # noqa: S603
        [
            perf,
            "report",
            "--stdio",
            "-g",
            "-i",
            str(scenario_dir / "perf.data"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    (scenario_dir / "perf.report.txt").write_text(report.stdout + report.stderr)
    if report.returncode != 0:
        msg = f"perf report failed with {report.returncode}"
        raise RuntimeError(msg)

    inferno = _require_tool("inferno-collapse-perf")
    script = subprocess.Popen(  # noqa: S603
        [perf, "script", "-i", str(scenario_dir / "perf.data")],
        stdout=subprocess.PIPE,
        text=True,
    )
    folded = subprocess.run(  # noqa: S603
        [inferno],
        stdin=script.stdout,
        check=False,
        capture_output=True,
        text=True,
    )
    if script.stdout is not None:
        script.stdout.close()
    script_return = script.wait()
    (scenario_dir / "stacks.folded").write_text(folded.stdout)
    if script_return != 0 or folded.returncode != 0:
        msg = "perf script or inferno-collapse-perf failed"
        raise RuntimeError(msg)
    summarize_folded_file(
        scenario_dir / "stacks.folded",
        output=scenario_dir / "summary.json",
    )


def _run_py_spy(
    scenario: TeeProfileScenario,
    *,
    scenario_dir: pth.Path,
) -> dict[str, object]:
    """Run the optional Python-first profiler for corroboration."""
    py_spy = _require_tool("py-spy")
    raw_path = scenario_dir / "pyspy.raw"
    command = [
        py_spy,
        "record",
        "--native",
        "--format",
        "raw",
        "--output",
        str(raw_path),
        "--",
        *_worker_command(scenario),
        "--output",
        str(scenario_dir / "worker-result.json"),
    ]
    completed = subprocess.run(command, check=False)  # noqa: S603
    if completed.returncode != 0:
        msg = f"py-spy failed for {scenario.name} with {completed.returncode}"
        raise RuntimeError(msg)
    return typ.cast(
        "dict[str, object]",
        json.loads((scenario_dir / "worker-result.json").read_text()),
    )


def run_profile_scenario(*, config: TeeProfileDriverConfig) -> typ.Mapping[str, object]:
    """Run one scenario, optionally under a profiler."""
    scenario = _scenario_by_name(config)
    scenario_dir = config.output_dir / scenario.name
    scenario_dir.mkdir(parents=True, exist_ok=True)
    _write_json(scenario_dir / "scenario.json", scenario.as_dict())
    _run_warmup(scenario, warmup_count=config.warmup_count)
    if config.profiler == "perf":
        return _run_perf(scenario, scenario_dir=scenario_dir, config=config)
    if config.profiler == "py-spy":
        return _run_py_spy(scenario, scenario_dir=scenario_dir)
    if config.profiler == "none":
        result = _run_worker_measured(scenario, scenario_dir=scenario_dir)
        notes = (
            "Profiler disabled; perf.data, perf.report.txt, stacks.folded, "
            "and summary.json were not generated.\n"
        )
        (scenario_dir / "notes.txt").write_text(notes)
        return result
    typ.assert_never(config.profiler)


def run_profile_matrix(
    *,
    config: TeeProfileDriverConfig,
) -> list[typ.Mapping[str, object]]:
    """Run all scenarios serially in the fixed matrix order."""
    results: list[typ.Mapping[str, object]] = []
    for scenario in default_tee_profile_scenarios(
        fixture_path=config.fixture_path,
        wrapped_fixture_path=config.wrapped_fixture_path,
        repeat_count=config.repeat_count,
    ):
        scenario_config = dc.replace(config, scenario_name=scenario.name)
        results.append(run_profile_scenario(config=scenario_config))
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
    parser.add_argument("--output-dir", type=pth.Path, default=_DEFAULT_OUTPUT_DIR)
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


def _config_from_args(args: argparse.Namespace) -> TeeProfileDriverConfig:
    """Convert parsed arguments to driver configuration."""
    return TeeProfileDriverConfig(
        fixture_path=args.fixture,
        wrapped_fixture_path=args.wrapped_fixture,
        output_dir=args.output_dir,
        profiler=args.profiler,
        warmup_count=args.warmup_count,
        repeat_count=args.repeat_count,
        perf_frequency=args.perf_frequency,
        perf_call_graph=args.perf_call_graph,
        scenario_name=getattr(args, "scenario", None),
    )


def main() -> int:
    """Run the tee profile driver CLI."""
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
