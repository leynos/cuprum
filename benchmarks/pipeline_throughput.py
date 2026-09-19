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
  --ci-ratchet --dry-run --output plan.json

``--ci-ratchet`` measures at its own worker-iteration count by default, so the
plan above is already the shape the CI job runs.
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
from benchmarks.benchmark_workload import (
    CI_RATCHET_WORKLOAD,
    SMOKE_WORKLOAD,
    THROUGHPUT_SWEEP_WORKLOAD,
)
from benchmarks.pipeline_throughput_runner import (
    build_hyperfine_command,
    render_prefixed_command,
    run_pipeline_benchmarks,
)
from benchmarks.pipeline_throughput_scenarios import (
    CI_RATCHET_WORKER_ITERATIONS,
    default_pipeline_scenarios,
)
from cuprum import is_rust_available

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# The help text is asserted semantically rather than by snapshot, and argparse
# reflows a multi-paragraph `description` into one unreadable block, so the
# parser's own description is a separate one-line literal rather than
# `__doc__`: the module docstring explains the workloads, and this names the
# command.
_CLI_DESCRIPTION = "Benchmark end-to-end pipeline throughput with hyperfine."

#: Worker iterations for the throughput sweep and the smoke matrix, matching
#: `PipelineBenchmarkConfig`'s own default. The ratchet workload overrides
#: this; see `_resolve_worker_iterations`.
_DEFAULT_WORKER_ITERATIONS = 20

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
        help="Use the reduced payload tiers for fast validation.",
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
        default=None,
        help=(
            "Number of pipeline executions inside each measured worker "
            "process. Defaults to the workload's own count: "
            f"{CI_RATCHET_WORKER_ITERATIONS} for --ci-ratchet, whose samples "
            "are only comparable at that count, and "
            f"{_DEFAULT_WORKER_ITERATIONS} otherwise."
        ),
    )
    return parser.parse_args(argv)


def _resolve_worker_iterations(args: argparse.Namespace) -> int:
    """Return the worker iteration count for the selected workload."""
    if args.worker_iterations is not None:
        return typ.cast("int", args.worker_iterations)
    # An omitted `--worker-iterations` is `None` rather than a number, so the
    # ratchet workload can default to the count its samples are recorded at
    # while every other workload keeps the throughput sweep's count.
    if args.ci_ratchet:
        return CI_RATCHET_WORKER_ITERATIONS
    return _DEFAULT_WORKER_ITERATIONS


def _resolve_workload(args: argparse.Namespace) -> str:
    """Return the workload identifier for the selected flags.

    The identifiers are recorded in the plan so that a summary rendering the
    plan can name the workload that produced it; the scenario shape alone
    cannot distinguish a smoke matrix from a ratchet one beyond its payload
    labels.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed arguments whose workload flags select the matrix.

    Returns
    -------
    str
        The identifier recorded in the plan the run writes.
    """
    if args.ci_ratchet:
        return CI_RATCHET_WORKLOAD
    if args.smoke:
        return SMOKE_WORKLOAD
    return THROUGHPUT_SWEEP_WORKLOAD


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
        worker_iterations=_resolve_worker_iterations(args),
        workload=_resolve_workload(args),
    )
    run_pipeline_benchmarks(config=config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
