"""Append a main-branch benchmark run to the rolling baseline history.

Run by every push to `main`, whatever the ratchet decided. Recording only
the runs that passed is what biased the old single-sample baseline towards
the low tail of the noise: a measurement faster than the bar was always
accepted, and the slower measurements that would have corrected it were the
ones rejected. A window fed by passing runs alone inherits that bias.

The command always writes an output file, even when there is nothing to
append. A run whose benchmark step died before producing candidate JSON
carries the previous window forward unchanged rather than publishing an
artefact with no history in it, which the next run would read as "no
history" and silently fall back to a single sample.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib as pth
import typing as typ

from benchmarks.benchmark_profile import IncompatibleBenchmarkProfileError
from benchmarks.ratchet_history import (
    DEFAULT_WINDOW_SIZE,
    BaselineHistory,
    BaselineHistoryReadError,
    HistorySample,
    load_history,
    write_history,
)
from benchmarks.ratchet_ratios import (
    load_plan,
    load_throughput,
    profile_metadata,
    run_ratios,
)
from benchmarks.ratchet_types import BenchmarkRunPayload

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_logger = logging.getLogger(__name__)


class _PositiveWindowArgumentError(argparse.ArgumentTypeError):
    """Argparse error for a non-positive history window."""

    def __init__(self) -> None:
        super().__init__("window must be at least 1")


def _positive_window(value: str) -> int:
    """Parse a strictly positive history window for ``argparse``."""
    window = int(value)
    if window < 1:
        raise _PositiveWindowArgumentError
    return window


def _candidate_sample(
    *,
    plan_path: pth.Path,
    throughput_path: pth.Path,
    commit: str,
    run_id: str,
) -> HistorySample | None:
    """Build a history sample from a completed run, or ``None`` when it cannot.

    Returning ``None`` rather than raising keeps a malformed or missing
    measurement from destroying the window that already exists: the caller
    carries the previous samples forward and the next run tries again.

    Returns
    -------
    HistorySample | None
        The run's sample, or ``None`` when it cannot be safely derived.
    """
    if not plan_path.is_file() or not throughput_path.is_file():
        _logger.warning(
            "no candidate benchmark output to record: plan=%s throughput=%s",
            plan_path,
            throughput_path,
        )
        return None
    try:
        plan = load_plan(plan_path)
        payload = BenchmarkRunPayload(
            plan=plan,
            throughput=load_throughput(throughput_path),
            context_name="candidate",
        )
        version, worker_iterations = profile_metadata(plan)
        ratios = run_ratios(payload)
    except (
        IncompatibleBenchmarkProfileError,
        json.JSONDecodeError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        _logger.warning("not recording this run's benchmark sample: %s", exc)
        return None
    return HistorySample(
        commit=commit,
        run_id=run_id,
        benchmark_profile_version=version,
        worker_iterations=worker_iterations,
        ratios=ratios,
    )


def _parse_args(argv: cabc.Sequence[str] | None) -> argparse.Namespace:
    """Parse command-line arguments for the history recorder."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        type=pth.Path,
        help="Existing history to extend. An absent file starts a new window.",
    )
    parser.add_argument("--candidate-plan", type=pth.Path, required=True)
    parser.add_argument("--candidate-throughput", type=pth.Path, required=True)
    parser.add_argument(
        "--commit",
        default="unknown",
        help="Commit the sample was measured at; provenance only.",
    )
    parser.add_argument(
        "--run-id",
        default="unknown",
        help="Workflow run that measured the sample; provenance only.",
    )
    parser.add_argument(
        "--window",
        type=_positive_window,
        default=DEFAULT_WINDOW_SIZE,
        help="How many of the most recent samples to keep.",
    )
    parser.add_argument("--output", type=pth.Path, required=True)
    return parser.parse_args(argv)


def _load_history_or_empty(path: pth.Path | None) -> BaselineHistory:
    """Load an optional history, treating only its intentional absence as empty."""
    if path is None or not path.is_file():
        _logger.info("no baseline history is available; starting a new window")
        return BaselineHistory()
    return load_history(path)


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Append this run's sample to the history and return an exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    try:
        history = _load_history_or_empty(args.history)
    except BaselineHistoryReadError:
        _logger.exception("failed to load baseline history")
        return 2
    sample = _candidate_sample(
        plan_path=args.candidate_plan,
        throughput_path=args.candidate_throughput,
        commit=args.commit,
        run_id=args.run_id,
    )
    updated = (
        history if sample is None else history.appended(sample, window_size=args.window)
    )
    try:
        write_history(history=updated, output_path=args.output)
    except OSError:
        _logger.exception("failed to write baseline history")
        return 2
    _logger.info(
        "baseline history now holds %d sample(s) at %s",
        len(updated.samples),
        args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
