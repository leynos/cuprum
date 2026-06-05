"""Unit tests for Python-versus-Rust benchmark comparison reporting."""

from __future__ import annotations

import json
import typing as typ

import pytest

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.python_vs_rust_comparison_report import (
    BenchmarkComparisonRow,
    RatchetStatus,
    compare_candidate_backend_results,
    load_ratchet_report,
    render_summary_markdown,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


def _scenario_payload(
    *,
    name: str,
    backend: str,
    **overrides: object,
) -> dict[str, object]:
    """Return a benchmark scenario payload."""
    defaults: dict[str, object] = {
        "payload_bytes": 1024,
        "stages": 2,
        "with_line_callbacks": False,
    }
    return {
        "name": name,
        "backend": backend,
        **defaults,
        **overrides,
    }


def _candidate_plan_payload() -> dict[str, object]:
    """Return a filtered candidate plan payload with paired backends."""
    return {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "worker_iterations": 20,
        "dry_run": True,
        "rust_available": True,
        "command": ["hyperfine", "placeholder"],
        "scenarios": [
            _scenario_payload(name="python-small-single-nocb", backend="python"),
            _scenario_payload(name="rust-small-single-nocb", backend="rust"),
            _scenario_payload(
                name="python-small-single-cb",
                backend="python",
                with_line_callbacks=True,
            ),
            _scenario_payload(
                name="rust-small-single-cb",
                backend="rust",
                with_line_callbacks=True,
            ),
        ],
    }


def _candidate_throughput_payload() -> dict[str, object]:
    """Return candidate throughput results aligned with the plan payload."""
    return {
        "results": [
            {"command": "python-small-single-nocb", "mean": 0.42},
            {"command": "rust-small-single-nocb", "mean": 0.21},
            {"command": "python-small-single-cb", "mean": 0.66},
            {"command": "rust-small-single-cb", "mean": 0.33},
        ],
    }


def _write_json(
    *,
    tmp_path: pth.Path,
    filename: str,
    payload: dict[str, object],
) -> pth.Path:
    """Write one JSON fixture to a temp file."""
    path = tmp_path / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_compare_candidate_backend_results_builds_sorted_rows() -> None:
    """Matched Python and Rust rows should produce deterministic comparisons."""
    report = compare_candidate_backend_results(
        plan_payload=_candidate_plan_payload(),
        throughput_payload=_candidate_throughput_payload(),
    )

    assert [row.comparison_id for row in report.rows] == [
        "small-single-cb",
        "small-single-nocb",
    ]
    assert report.summary.row_count == 2
    assert report.summary.rust_wins == 2
    assert report.summary.python_wins == 0
    assert report.summary.ties == 0

    first_row = report.rows[0]
    assert first_row == BenchmarkComparisonRow(
        comparison_id="small-single-cb",
        python_scenario_name="python-small-single-cb",
        rust_scenario_name="rust-small-single-cb",
        python_mean=0.66,
        rust_mean=0.33,
        speedup_ratio=2.0,
        faster_backend="rust",
    )


def test_compare_candidate_backend_results_treats_close_means_as_ties() -> None:
    """Means within FLOAT_TOLERANCE should be treated as ties."""
    plan_payload = _candidate_plan_payload()
    throughput_payload = _candidate_throughput_payload()
    results = typ.cast("list[dict[str, object]]", throughput_payload["results"])
    # Make the first pair (python-small-single-nocb and rust-small-single-nocb) a tie
    results[0]["mean"] = 0.42
    results[1]["mean"] = 0.42 + 5e-13

    report = compare_candidate_backend_results(
        plan_payload=plan_payload,
        throughput_payload=throughput_payload,
    )

    # Results sorted by comparison_id: small-single-cb before small-single-nocb
    assert report.rows[1].comparison_id == "small-single-nocb"
    assert report.rows[1].faster_backend == "tie"
    assert report.summary.ties == 1
    assert report.summary.rust_wins == 1
    assert report.summary.python_wins == 0


def test_compare_candidate_backend_results_rejects_missing_rust_pair() -> None:
    """Every comparison group must include both Python and Rust scenarios."""
    plan_payload = _candidate_plan_payload()
    scenarios = typ.cast("list[object]", plan_payload["scenarios"])
    plan_payload["scenarios"] = [scenarios[0]]
    throughput_results = typ.cast(
        "list[object]",
        _candidate_throughput_payload()["results"],
    )
    throughput_payload = {
        "results": [throughput_results[0]],
    }

    with pytest.raises(ValueError, match="missing Rust scenario"):
        compare_candidate_backend_results(
            plan_payload=plan_payload,
            throughput_payload=throughput_payload,
        )


def test_compare_candidate_backend_results_rejects_duplicate_backend() -> None:
    """Each comparison group must not contain duplicate backend entries."""
    plan_payload = _candidate_plan_payload()
    scenarios = typ.cast("list[dict[str, object]]", plan_payload["scenarios"])
    # Both scenarios should have the same comparison_id (after stripping backend prefix)
    first_scenario = dict(scenarios[0])
    # Keep the same name to ensure both map to the same comparison_id
    plan_payload["scenarios"] = [scenarios[0], first_scenario]

    throughput_payload = _candidate_throughput_payload()
    results = typ.cast("list[dict[str, object]]", throughput_payload["results"])
    first_result = dict(results[0])
    throughput_payload["results"] = [results[0], first_result]

    with pytest.raises(ValueError, match=r"duplicate.*python.*scenario"):
        compare_candidate_backend_results(
            plan_payload=plan_payload,
            throughput_payload=throughput_payload,
        )


def test_compare_candidate_backend_results_rejects_invalid_backend() -> None:
    """Scenario backend values must be 'python' or 'rust'."""
    plan_payload = _candidate_plan_payload()
    scenarios = typ.cast("list[dict[str, object]]", plan_payload["scenarios"])
    invalid_scenario = dict(scenarios[0])
    invalid_scenario["backend"] = "invalid-backend"
    plan_payload["scenarios"] = [invalid_scenario]

    throughput_payload = _candidate_throughput_payload()
    results = typ.cast("list[dict[str, object]]", throughput_payload["results"])
    throughput_payload["results"] = [results[0]]

    with pytest.raises(ValueError, match="must be either 'python' or 'rust'"):
        compare_candidate_backend_results(
            plan_payload=plan_payload,
            throughput_payload=throughput_payload,
        )


@pytest.mark.parametrize(
    ("payload", "expected_match"),
    [
        pytest.param(
            {"passed": "yes", "comparison_performed": True, "baseline_available": True},
            "boolean passed field",
            id="passed",
        ),
        pytest.param(
            {"passed": True, "comparison_performed": "yes", "baseline_available": True},
            "non-boolean 'comparison_performed'",
            id="comparison_performed",
        ),
        pytest.param(
            {"passed": True, "comparison_performed": True, "baseline_available": "yes"},
            "non-boolean 'baseline_available'",
            id="baseline_available",
        ),
    ],
)
def test_load_ratchet_report_rejects_non_boolean_field(
    tmp_path: pth.Path,
    payload: dict[str, object],
    expected_match: str,
) -> None:
    """Ratchet report boolean fields must be booleans."""
    ratchet_path = _write_json(
        tmp_path=tmp_path,
        filename="ratchet-report.json",
        payload=payload,
    )
    with pytest.raises(TypeError, match=expected_match):
        load_ratchet_report(ratchet_path)


@pytest.mark.parametrize(
    ("ratchet_payload", "expected_status", "expected_fragment"),
    [
        (
            {"passed": True, "comparison_performed": True, "baseline_available": True},
            RatchetStatus(status="passed", detail="Rust regression ratchet passed."),
            "Rust regression ratchet passed.",
        ),
        (
            {"passed": False, "comparison_performed": True, "baseline_available": True},
            RatchetStatus(status="failed", detail="Rust regression ratchet failed."),
            "Rust regression ratchet failed.",
        ),
        (
            {
                "passed": True,
                "comparison_performed": False,
                "baseline_available": False,
                "reason": "no_previous_main_benchmark_baseline",
            },
            RatchetStatus(
                status="skipped",
                detail=(
                    "Rust regression ratchet skipped: no previous successful "
                    "main baseline artefact."
                ),
            ),
            "Rust regression ratchet skipped",
        ),
    ],
)
def test_load_ratchet_report_interprets_known_statuses(
    tmp_path: pth.Path,
    ratchet_payload: dict[str, object],
    expected_status: RatchetStatus,
    expected_fragment: str,
) -> None:
    """Ratchet report metadata should be mapped into a summary status."""
    path = _write_json(
        tmp_path=tmp_path,
        filename="ratchet-report.json",
        payload=ratchet_payload,
    )

    status = load_ratchet_report(path)

    assert status == expected_status
    assert expected_fragment in status.detail


def test_render_summary_markdown_includes_table_and_ratchet_status() -> None:
    """Rendered markdown should be suitable for the workflow summary."""
    report = compare_candidate_backend_results(
        plan_payload=_candidate_plan_payload(),
        throughput_payload=_candidate_throughput_payload(),
    )

    markdown = render_summary_markdown(
        report=report,
        ratchet_status=RatchetStatus(
            status="passed",
            detail="Rust regression ratchet passed.",
        ),
    )

    assert "## Python vs Rust benchmark comparison" in markdown
    assert "Rust regression ratchet passed." in markdown
    assert (
        "| Scenario | Python mean (s) | Rust mean (s) | Speedup | Faster backend |"
        in markdown
    )
    assert "| `small-single-nocb` | 0.420000 | 0.210000 | 2.00x | rust |" in markdown
