r"""Benchmark end-to-end pipeline throughput with hyperfine.

Renders a scenario matrix into prefixed worker commands and hands them to
hyperfine, which times each one; the dry-run mode writes the same plan as
JSON instead of executing it. Three workloads are available, and the
matrix they select is the only thing that varies between them:

- the throughput sweep (the default), which covers three payload tiers;
- ``--smoke``, the same shape at reduced payloads, for fast validation;
- ``--ci-ratchet``, the single large payload the CI ratchet compares
  between runs, where streaming dominates the fixed per-run cost.

``--smoke`` and ``--ci-ratchet`` select contradictory payloads and are
mutually exclusive on the command line; ``default_pipeline_scenarios``
rejects the pair too, so a caller that bypasses argparse still cannot ask
for both.

Example
-------
uv run python benchmarks/pipeline_throughput.py \\
  --ci-ratchet --dry-run --worker-iterations 5 --output plan.json
"""

from __future__ import annotations

import argparse
import pathlib as pth
import typing as typ

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

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# The help text is asserted semantically rather than by snapshot, and argparse
# reflows a multi-paragraph `description` into one unreadable block, so the
# parser's own description is a separate one-line literal rather than
# `__doc__`: the module docstring explains the workloads, and this names the
# command.
_CLI_DESCRIPTION = "Benchmark end-to-end pipeline throughput with hyperfine."

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


def _parse_args(argv: cabc.Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the throughput runner."""
    parser = argparse.ArgumentParser(description=_CLI_DESCRIPTION)
    parser.add_argument(
        "--output",
        type=pth.Path,
        required=True,
        help="Path for hyperfine JSON output (or dry-run plan output).",
    )
    workloads = parser.add_mutually_exclusive_group()
    workloads.add_argument(
        "--smoke",
        action="store_true",
        help="Use a 1 KB payload and fewer iterations for fast validation.",
    )
    workloads.add_argument(
        "--ci-ratchet",
        action="store_true",
        help=(
            "Build the single-payload CI ratchet matrix instead of the "
            "throughput sweep, so streaming dominates the fixed per-run cost."
        ),
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
    return parser.parse_args(argv)


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run the benchmark CLI entry point.

    Parameters
    ----------
    argv : collections.abc.Sequence[str] | None
        Optional CLI argument sequence; when ``None`` the process
        arguments are parsed.

    Returns
    -------
    int
        The process exit code.
    """
    args = _parse_args(argv)
    rust_available = is_rust_available()
    scenarios = default_pipeline_scenarios(
        smoke=args.smoke,
        include_rust=rust_available,
        ci_ratchet=args.ci_ratchet,
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
