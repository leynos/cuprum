"""Benchmark end-to-end pipeline throughput with hyperfine."""

from __future__ import annotations

import argparse
import pathlib as pth

from benchmarks._benchmark_types import (
    HyperfineConfig,
    PipelineBenchmarkConfig,
    PipelineBenchmarkRunResult,
    PipelineBenchmarkScenario,
    PipelineBenchmarkScenarioDict,
)
from benchmarks.pipeline_throughput_runner import (
    build_hyperfine_command,
    render_prefixed_command,
    run_pipeline_benchmarks,
)
from benchmarks.pipeline_throughput_scenarios import default_pipeline_scenarios
from cuprum import is_rust_available

__all__ = [
    "HyperfineConfig",
    "PipelineBenchmarkConfig",
    "PipelineBenchmarkRunResult",
    "PipelineBenchmarkScenario",
    "PipelineBenchmarkScenarioDict",
    "build_hyperfine_command",
    "default_pipeline_scenarios",
    "main",
    "render_prefixed_command",
    "run_pipeline_benchmarks",
]


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
    parser.add_argument(
        "--worker-iterations",
        type=int,
        default=20,
        help="Number of pipeline executions inside each measured worker process.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the benchmark CLI entry point."""
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
        worker_iterations=args.worker_iterations,
    )
    run_pipeline_benchmarks(config=config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
