"""Contracts for the gate arithmetic over paired compiler-cache evidence.

The reader's own records are validated in
`tests/test_ci_compiler_cache_evidence.py`. These tests feed the summary
deliberately chosen observations and assert the arithmetic that decides an
adoption: independent job pairs, failed jobs excluded from savings, the
restore-plus-save total, and the signed incremental cost.
"""

from __future__ import annotations

import json
import typing as typ

import pytest

from scripts import compiler_cache_records as records_reader
from scripts import compiler_cache_report as report
from scripts import compiler_cache_schema as schema
from scripts import compiler_cache_summary as summary_reader
from tests.helpers.ci_compiler_evidence import record_fixture

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

JOB_NAME = "Extension-gated tests"


def _record(
    *,
    run_id: str,
    backend: str,
    hits: int,
    restore: float | None = 20.0,
    save: float | None = 20.0,
    bracket: str = "extension",
    leg: str = "none",
    job: str = "extension-tests",
    state: str = "ok",
    restore_hit: bool | None = True,
) -> dict[str, object]:
    """Return one record with the counters and timings a pair needs."""
    counters = dict.fromkeys(schema.COUNTERS, None)
    counters["compile_requests"] = 5
    counters["cache_hits"] = hits
    return record_fixture(
        run_id=run_id,
        backend=backend,
        counters=counters,
        restore_hit=restore_hit,
        restore_seconds=restore,
        save_seconds=save,
        bracket=bracket,
        matrix_leg=leg,
        job=job,
        state=state,
        family="py3.13-debug",
    )


def _jobs(
    run_ids: cabc.Iterable[int], seconds: float, *, conclusion: str = "success"
) -> list[dict[str, object]]:
    """Return one API job per run id, all lasting ``seconds``."""
    return [
        {
            "run_id": run,
            "run_attempt": 1,
            "name": JOB_NAME,
            "started_at": "2026-11-22T00:00:00Z",
            "completed_at": (
                f"2026-11-22T00:{int(seconds // 60):02d}:{int(seconds % 60):02d}Z"
            ),
            "conclusion": conclusion,
        }
        for run in run_ids
    ]


NAMES = {"extension-tests": JOB_NAME}


def _family(summary: cabc.Mapping[str, object], index: int = 0) -> dict[str, object]:
    """Return one family from a summary, narrowed for subscripting."""
    families = typ.cast("list[dict[str, object]]", summary["families"])
    return families[index]


def _decisions(summary: cabc.Mapping[str, object]) -> list[dict[str, object]]:
    """Return the gate decisions from a summary, narrowed for subscripting."""
    gate = typ.cast("dict[str, object]", summary["gate"])
    return typ.cast("list[dict[str, object]]", gate["decisions"])


def _summary(
    records: cabc.Sequence[dict[str, object]],
    jobs: cabc.Sequence[dict[str, object]],
    console: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a summary from fixtures through the real validator."""
    validated = [records_reader.validate_record(record) for record in records]
    return summary_reader.build_summary(validated, jobs, NAMES, console)


def _console(**overrides: object) -> dict[str, object]:
    """Return a console observation with the gate fields set."""
    document: dict[str, object] = {
        "schema_version": schema.SCHEMA_VERSION,
        "observed_at": "2026-11-22",
        "branch_protection": "enabled",
        "storage_gb": 30,
        "eviction_days": 7,
        "billing_rounding": "unknown",
        "initial_population_seconds": 60,
        "incremental_transfer_seconds": 5,
        "incremental_cost_seconds": 0,
        "note": "test",
    }
    document.update(overrides)
    return document


def test_the_transfer_is_the_sum_of_restore_and_save_per_job() -> None:
    """A median of individual timings would halve the cost a migration removes."""
    records = [
        _record(run_id="100", backend="local", hits=4),
        _record(run_id="200", backend="gha", hits=6, restore=None, save=None),
    ]
    summary = _summary(records, _jobs([100, 200], 240))
    family = _family(summary)
    assert family["median_transfer_seconds"] == pytest.approx(40.0), (
        f"20 s restore plus 20 s save must report 40 s, got {family}"
    )


def test_a_control_without_a_save_time_keeps_the_transfer_unknown() -> None:
    """A pull-request control never saves; inventing zero would understate cost."""
    records = [
        _record(run_id="100", backend="local", hits=4, save=None),
        _record(run_id="200", backend="gha", hits=6, restore=None, save=None),
    ]
    summary = _summary(records, _jobs([100, 200], 240))
    family = _family(summary)
    assert family["median_transfer_seconds"] is None, (
        f"a missing save time must keep the total unknown, got {family}"
    )


def test_failed_jobs_are_counted_but_never_pair_savings() -> None:
    """A failing candidate must not satisfy the gate through a shorter job."""
    records = [
        _record(run_id=str(100 + index), backend="local", hits=4) for index in range(5)
    ]
    records += [
        _record(run_id=str(200 + index), backend="gha", hits=6) for index in range(5)
    ]
    jobs = _jobs([*range(100, 105), *range(200, 205)], 240)
    for job in jobs:
        if int(typ.cast("str", job["run_id"])) >= 200:
            job["conclusion"] = "failure"
    summary = _summary(records, jobs)
    family = _family(summary)
    assert family["failures"] == 5, (
        f"each failed candidate must be counted, got {family}"
    )
    assert family["pairs"] == 0, (
        f"a failed candidate must contribute no saving pair, got {family}"
    )
    decision = _decisions(summary)[0]
    assert decision["verdict"] == "insufficient-evidence", decision


def test_two_brackets_in_one_job_are_one_independent_pair() -> None:
    """Bracket pairs would multiply the sample and satisfy the gate early."""
    records: list[dict[str, object]] = [
        _record(run_id="100", backend="local", hits=4, bracket="typecheck"),
        _record(run_id="100", backend="local", hits=4, bracket="python-tests"),
        _record(
            run_id="200",
            backend="gha",
            hits=6,
            bracket="typecheck",
            restore=None,
            save=None,
        ),
        _record(
            run_id="200",
            backend="gha",
            hits=6,
            bracket="python-tests",
            restore=None,
            save=None,
        ),
    ]
    jobs: list[dict[str, object]] = [
        {
            "run_id": run,
            "run_attempt": 1,
            "name": JOB_NAME,
            "started_at": "2026-11-22T00:00:00Z",
            "completed_at": "2026-11-22T00:04:00Z",
            "conclusion": "success",
        }
        for run in (100, 200)
    ]
    summary = _summary(records, jobs)
    family = _family(summary)
    assert family["pairs"] == 1, (
        f"two brackets of one job are one independent pair, got {family}"
    )
    assert family["independent_job_pairs"] == 1


def test_job_cost_is_charged_once_for_several_brackets() -> None:
    """Three brackets in a 100-second job must not report 300 job-seconds."""
    records = [
        _record(run_id="100", backend="local", hits=4, bracket=name)
        for name in ("typecheck", "python-tests", "extension")
    ]
    summary = _summary(records, _jobs([100], 100))
    cost = typ.cast("dict[str, object]", summary["cost"])
    assert cost["paid_job_seconds_by_workflow"] == {"ci.yml": 100.0}, (
        f"one job with three brackets must be charged once, got {cost}"
    )


def test_a_negative_incremental_cost_is_accepted() -> None:
    """The gate requires non-positive cost, not exactly zero."""
    records = [
        _record(run_id=str(100 + index), backend="local", hits=4) for index in range(5)
    ]
    records += [
        _record(
            run_id=str(200 + index),
            backend="gha",
            hits=6,
            restore=None,
            save=None,
        )
        for index in range(5)
    ]
    jobs = _jobs([*range(100, 105), *range(200, 205)], 240)
    for job in jobs:
        if int(typ.cast("str", job["run_id"])) >= 200:
            job["completed_at"] = "2026-11-22T00:03:15Z"
    summary = _summary(records, jobs, _console(incremental_cost_seconds=-10))
    decision = _decisions(summary)[0]
    assert decision["incremental_cost_seconds"] == pytest.approx(-10.0), decision
    assert decision["verdict"] == "adopt", decision


def test_a_push_record_with_an_unknown_pr_is_not_a_pull_request_consumer() -> None:
    """The writer's `unknown` sentinel must not pass a truthiness check."""
    records = [
        _record(run_id="100", backend="local", hits=4, restore_hit=True),
    ]
    records[0]["pr_number"] = "unknown"
    records[0]["event_class"] = "push"
    summary = _summary(records, _jobs([100, 200], 240))
    observation = typ.cast("list[dict[str, object]]", summary["observations"])[0]
    assert observation["observation_class"] == "fresh-consumer", observation


def test_brackets_of_one_pull_request_run_are_the_first_consumer() -> None:
    """A second bracket of the same run is not a later push."""
    records: list[dict[str, object]] = [
        _record(run_id="100", backend="local", hits=4, bracket="typecheck"),
        _record(run_id="100", backend="local", hits=4, bracket="python-tests"),
    ]
    summary = _summary(records, _jobs([100, 200], 240))
    observations = typ.cast("list[dict[str, object]]", summary["observations"])
    classes = [observation["observation_class"] for observation in observations]
    assert classes == ["first-pr-consumer", "first-pr-consumer"], (
        f"both brackets of the first run are that PR's first consumer, got {classes}"
    )


def test_the_summary_reports_insufficient_evidence_without_pairs() -> None:
    """No records must not read as an adopt decision."""
    summary = summary_reader.build_summary([], [], {}, None)
    gate = typ.cast("dict[str, object]", summary["gate"])
    cost = typ.cast("dict[str, object]", summary["cost"])
    assert gate["decisions"] == [], "no families means no decisions"
    assert cost["billing"] == "proxy", (
        "runner-seconds are a proxy unless billing is verified"
    )
    assert cost["paid_job_seconds_by_workflow"] is None, (
        "an absent jobs document must leave seconds unknown"
    )
    assert cost["initial_population_seconds"] is None, (
        "an absent console observation must leave cost unknown, not zero"
    )


def test_the_markdown_report_states_the_failure() -> None:
    """A report with no observations must say so rather than render a blank table."""
    rendered = report.render_markdown(summary_reader.build_summary([], [], {}, None))
    assert "insufficient-evidence" in rendered, rendered


def test_non_finite_measurements_are_rejected() -> None:
    """`1e309` parses as infinity and must not reach the report."""
    document = record_fixture(restore_seconds=float("inf"))
    with pytest.raises(records_reader.InvalidEvidenceError, match="restore_seconds"):
        records_reader.validate_record(document)


def test_an_identifier_of_the_wrong_type_is_rejected() -> None:
    """A list in a family field would flatten into a plausible label."""
    document = record_fixture(family=["py3.13-debug"])
    with pytest.raises(
        records_reader.InvalidEvidenceError, match="family must be a string"
    ):
        records_reader.validate_record(document)


def test_two_matrix_legs_of_one_bracket_are_distinct_records(
    tmp_path: Path,
) -> None:
    """The matrix records the same job and bracket once per leg."""
    path = tmp_path / "records.jsonl"
    first = record_fixture(matrix_leg="3.12")
    second = record_fixture(matrix_leg="3.14")
    path.write_text(
        f"{json.dumps(first)}\n{json.dumps(second)}\n",
        encoding="utf-8",
    )
    assert len(records_reader.load_records([path])) == 2, (
        "records from distinct matrix legs must both load"
    )
