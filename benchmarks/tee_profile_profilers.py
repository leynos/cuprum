"""Profiler orchestration for Cuprum tee hot-path profiling."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # noqa: S404  # profiling driver intentionally invokes tools
import time
import typing as typ

from benchmarks.summarize_folded import summarize_folded_file
from benchmarks.tee_profile_scenarios import (
    ProfilerName,
    TeeProfileDriverConfig,
    TeeProfileScenario,
    _worker_command,
)
from benchmarks.tee_profile_worker import TeeProfileWorkerResult, run_tee_profile_worker

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth


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
    from benchmarks.tee_profile_driver import _write_json

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
    from benchmarks.tee_profile_driver import _write_json

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
    with subprocess.Popen(  # noqa: S603
        [perf, "script", "-i", str(scenario_dir / "perf.data")],
        stdout=subprocess.PIPE,
        text=True,
    ) as script:
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


class ProfilerAdapter(typ.Protocol):
    """Interface for profiler orchestration strategies."""

    def run(
        self,
        scenario: TeeProfileScenario,
        *,
        scenario_dir: pth.Path,
        config: TeeProfileDriverConfig,
    ) -> cabc.Mapping[str, object]:
        """Execute the scenario under this profiler and return the result."""
        raise NotImplementedError


class _NoneProfiler:
    @staticmethod
    def run(
        scenario: TeeProfileScenario,
        *,
        scenario_dir: pth.Path,
        config: TeeProfileDriverConfig,
    ) -> cabc.Mapping[str, object]:
        """Execute the scenario without profiler sampling."""
        result = _run_worker_measured(scenario, scenario_dir=scenario_dir)
        notes = (
            "Profiler disabled; perf.data, perf.report.txt, stacks.folded, "
            "and summary.json were not generated.\n"
        )
        (scenario_dir / "notes.txt").write_text(notes)
        return result


class _PerfProfiler:
    @staticmethod
    def run(
        scenario: TeeProfileScenario,
        *,
        scenario_dir: pth.Path,
        config: TeeProfileDriverConfig,
    ) -> cabc.Mapping[str, object]:
        """Execute the scenario under Linux perf."""
        return _run_perf(scenario, scenario_dir=scenario_dir, config=config)


class _PySpyProfiler:
    @staticmethod
    def run(
        scenario: TeeProfileScenario,
        *,
        scenario_dir: pth.Path,
        config: TeeProfileDriverConfig,
    ) -> cabc.Mapping[str, object]:
        """Execute the scenario under py-spy."""
        return _run_py_spy(scenario, scenario_dir=scenario_dir)


def _profiler_for(name: ProfilerName) -> ProfilerAdapter:
    """Return the adapter for the requested profiler."""
    if name == "none":
        return _NoneProfiler()
    if name == "perf":
        return _PerfProfiler()
    if name == "py-spy":
        return _PySpyProfiler()
    typ.assert_never(name)
