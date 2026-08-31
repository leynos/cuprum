"""Unit tests for the main-branch baseline history recorder.

The recorder runs on every push to `main`, including the runs whose ratchet
failed. Two of its properties are load-bearing and neither is obvious from
the happy path:

- it records a sample regardless of the verdict, because a window fed only
  by passing runs is biased towards the low tail of the noise; and
- it always writes an output file, because a run that skipped the write
  would publish an artefact with no history in it, which the next run reads
  as "no history" and silently degrades to a single-sample bar.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

import pytest

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ratchet_history import BaselineHistory, HistorySample, load_history
from benchmarks.update_baseline_history import main as record_sample

if typ.TYPE_CHECKING:
    import pathlib as pth

SCENARIO = "small-single-nocb"
WORKER_ITERATIONS = 20


@dc.dataclass(frozen=True, slots=True)
class Candidate:
    """The pair of files a completed benchmark run leaves behind."""

    plan: pth.Path
    throughput: pth.Path


def _write_candidate(directory: pth.Path, *, ratio: float) -> Candidate:
    """Write candidate plan and throughput JSON realizing one ratio."""
    plan_path = directory / "candidate-plan.json"
    throughput_path = directory / "candidate-throughput.json"
    plan_path.write_text(
        json.dumps({
            "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
            "worker_iterations": WORKER_ITERATIONS,
            "scenarios": [
                {"name": f"python-{SCENARIO}", "backend": "python"},
                {"name": f"rust-{SCENARIO}", "backend": "rust"},
            ],
        }),
        encoding="utf-8",
    )
    throughput_path.write_text(
        json.dumps({
            "results": [
                {"command": f"python-{SCENARIO}", "mean": 1.0},
                {"command": f"rust-{SCENARIO}", "mean": ratio},
            ],
        }),
        encoding="utf-8",
    )
    return Candidate(plan=plan_path, throughput=throughput_path)


def _existing_history(path: pth.Path, *ratios: float) -> BaselineHistory:
    """Write and return a history of the given ratios."""
    history = BaselineHistory(
        samples=tuple(
            HistorySample(
                commit=f"commit-{index}",
                run_id=str(index),
                benchmark_profile_version=BENCHMARK_PROFILE_VERSION,
                worker_iterations=WORKER_ITERATIONS,
                ratios={SCENARIO: ratio},
            )
            for index, ratio in enumerate(ratios)
        )
    )
    path.write_text(
        json.dumps(history.as_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    return history


def _record(
    tmp_path: pth.Path,
    *,
    candidate: Candidate,
    history: pth.Path | None = None,
    window: int | None = None,
) -> tuple[int, pth.Path]:
    """Run the recorder CLI and return its exit code and output path."""
    output = tmp_path / "main-baseline-history.json"
    argv = [
        "--candidate-plan",
        str(candidate.plan),
        "--candidate-throughput",
        str(candidate.throughput),
        "--commit",
        "abcdef1234",
        "--run-id",
        "987654",
        "--output",
        str(output),
    ]
    if history is not None:
        argv.extend(["--history", str(history)])
    if window is not None:
        argv.extend(["--window", str(window)])
    return record_sample(argv), output


def test_a_completed_run_is_appended_with_its_provenance(tmp_path: pth.Path) -> None:
    """The sample carries the commit and run that measured it."""
    candidate = _write_candidate(tmp_path, ratio=1.25)

    exit_code, output = _record(tmp_path, candidate=candidate)

    assert exit_code == 0
    recorded = load_history(output)
    assert len(recorded.samples) == 1
    sample = recorded.samples[0]
    assert sample.ratios == {SCENARIO: pytest.approx(1.25)}
    assert (sample.commit, sample.run_id) == ("abcdef1234", "987654")


def test_a_regressed_measurement_is_recorded_like_any_other(
    tmp_path: pth.Path,
) -> None:
    """The recorder has no verdict to consult, and must not acquire one.

    This is the half of the fix the statistics cannot supply: filtering
    samples by whether their run passed is what biased the old baseline.
    """
    history_path = tmp_path / "existing.json"
    _existing_history(history_path, 1.0, 1.0, 1.0)
    candidate = _write_candidate(tmp_path, ratio=9.0)

    _, output = _record(tmp_path, candidate=candidate, history=history_path)

    assert load_history(output).ratios_for(SCENARIO) == (
        pytest.approx(1.0),
        pytest.approx(1.0),
        pytest.approx(1.0),
        pytest.approx(9.0),
    )


def test_the_window_is_pruned_to_its_size(tmp_path: pth.Path) -> None:
    """Recording past the window drops the oldest sample."""
    history_path = tmp_path / "existing.json"
    _existing_history(history_path, 1.0, 2.0, 3.0)
    candidate = _write_candidate(tmp_path, ratio=4.0)

    _, output = _record(tmp_path, candidate=candidate, history=history_path, window=3)

    assert load_history(output).ratios_for(SCENARIO) == (
        pytest.approx(2.0),
        pytest.approx(3.0),
        pytest.approx(4.0),
    )


@pytest.mark.parametrize("scenario", ["missing", "malformed"])
def test_an_unusable_run_carries_the_window_forward(
    tmp_path: pth.Path,
    scenario: str,
) -> None:
    """A run that measured nothing must not destroy what earlier runs measured.

    Writing no file at all would be worse than writing the old window: the
    artefact would publish without a history, and the next run would read
    that as a fresh start.
    """
    history_path = tmp_path / "existing.json"
    existing = _existing_history(history_path, 1.0, 1.1)
    if scenario == "missing":
        candidate = Candidate(
            plan=tmp_path / "absent-plan.json",
            throughput=tmp_path / "absent-throughput.json",
        )
    else:
        candidate = _write_candidate(tmp_path, ratio=1.0)
        candidate.plan.write_text("{ not json", encoding="utf-8")

    exit_code, output = _record(tmp_path, candidate=candidate, history=history_path)

    assert exit_code == 0, "an unmeasurable run must not fail the recorder"
    assert load_history(output) == existing


def test_a_first_run_writes_an_empty_but_valid_history(tmp_path: pth.Path) -> None:
    """With neither history nor measurement, the output must still parse."""
    exit_code, output = _record(
        tmp_path,
        candidate=Candidate(
            plan=tmp_path / "absent-plan.json",
            throughput=tmp_path / "absent-throughput.json",
        ),
    )

    assert exit_code == 0
    assert output.is_file()
    assert load_history(output) == BaselineHistory()
