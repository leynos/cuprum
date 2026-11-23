"""Contracts for compiler-cache classification and the summary's edges.

The gate arithmetic has its own module. These tests cover the labels the
reader assigns to observations, the strict edges of its input contract, and the
report it writes when no evidence exists.
"""

from __future__ import annotations

import json
import typing as typ

import pytest

from scripts import compiler_cache_records as records_reader
from scripts import compiler_cache_report as report
from scripts import compiler_cache_summary as summary_reader
from tests.helpers.ci_compiler_evidence import record_fixture
from tests.helpers.ci_compiler_gate import (
    Observation,
    console,
    decisions,
    family,
    jobs,
    observations,
    summary,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_a_push_record_with_an_unknown_pr_is_not_a_pull_request_consumer() -> None:
    """The writer's `unknown` sentinel must not pass a truthiness check."""
    records = [
        Observation(
            run_id="100",
            backend="local",
            hits=4,
            restore_hit=True,
            pr_number="unknown",
            event_class="push",
        ),
    ]
    built = summary(records, jobs([100], 240))
    assert observations(built)[0]["observation_class"] == "fresh-consumer", (
        f"a push run must not be a pull request, got {observations(built)}"
    )


def test_brackets_of_one_pull_request_run_are_the_first_consumer() -> None:
    """A second bracket of the same run is not a later push."""
    records: list[Observation] = [
        Observation(run_id="100", backend="local", hits=4, bracket="typecheck"),
        Observation(run_id="100", backend="local", hits=4, bracket="python-tests"),
    ]
    built = summary(records, jobs([100], 240))
    classes = [item["observation_class"] for item in observations(built)]
    assert classes == ["first-pr-consumer", "first-pr-consumer"], (
        f"both brackets of the first run are that PR's first consumer, got {classes}"
    )


def test_a_later_pull_request_run_is_a_subsequent_push() -> None:
    """A second run for the same PR is the case the experiment needs."""
    records = [
        Observation(run_id="100", backend="local", hits=4),
        Observation(run_id="200", backend="local", hits=6),
    ]
    built = summary(records, jobs([100, 200], 240))
    classes = [item["observation_class"] for item in observations(built)]
    assert classes == ["first-pr-consumer", "subsequent-pr-push"], (
        f"the second run of a PR must be a subsequent push, got {classes}"
    )


def test_a_cold_first_run_does_not_promote_the_next_warm_run() -> None:
    """Registration happens before the cold label, so the next run is later."""
    records = [
        Observation(run_id="100", backend="local", hits=0, restore_hit=False),
        Observation(run_id="200", backend="local", hits=6),
    ]
    built = summary(records, jobs([100, 200], 240))
    classes = [item["observation_class"] for item in observations(built)]
    assert classes == ["cold-or-new-family", "subsequent-pr-push"], (
        f"a cold first run must not let the next run look first, got {classes}"
    )


def test_the_summary_reports_insufficient_evidence_without_pairs() -> None:
    """No records must not read as an adopt decision."""
    built = summary_reader.build_summary([], [], {}, None)
    gate = typ.cast("dict[str, object]", built["gate"])
    cost = typ.cast("dict[str, object]", built["cost"])
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


def test_a_multiline_job_is_rejected() -> None:
    """A job key is a bounded identifier, not free text."""
    document = record_fixture(job="line one\nline two")
    with pytest.raises(
        records_reader.InvalidEvidenceError, match="job must be a bounded"
    ):
        records_reader.validate_record(document)


def test_two_matrix_legs_of_one_bracket_are_distinct_records(tmp_path: Path) -> None:
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


def test_a_boolean_console_schema_version_is_rejected(tmp_path: Path) -> None:
    """`true` equals 1 in Python, but not in the schema contract."""
    path = tmp_path / "console.json"
    document = console()
    document["schema_version"] = True
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(records_reader.InvalidEvidenceError, match="must be an integer"):
        records_reader.load_console(path)


def test_the_gate_decisions_name_each_family() -> None:
    """A decision that does not name its family cannot be reviewed."""
    records = [
        Observation(run_id="100", backend="local", hits=4),
        Observation(run_id="200", backend="gha", hits=6, restore=None, save=None),
    ]
    built = summary(records, jobs([100, 200], 240))
    assert decisions(built)[0]["family"] == family(built)["family"], (
        "the decision and its family must agree"
    )


def test_a_non_string_job_name_mapping_is_rejected(tmp_path: Path) -> None:
    """A numeric display-name template would select an unrelated job."""
    path = tmp_path / "names.json"
    path.write_text(json.dumps({"extension-tests": 3.13}), encoding="utf-8")
    with pytest.raises(records_reader.InvalidEvidenceError, match="must be strings"):
        records_reader.load_names(path)


def test_the_markdown_output_directory_is_created(tmp_path: Path) -> None:
    """A requested report must not fail on a missing parent directory."""
    records = tmp_path / "records"
    records.mkdir()
    output = tmp_path / "summary.json"
    markdown = tmp_path / "reports" / "summary.md"
    status = report.main([
        "--records",
        str(records),
        "--out",
        str(output),
        "--markdown",
        str(markdown),
    ])
    assert status == 0, f"the report command must succeed, got {status}"
    assert markdown.is_file(), "the report must be written where it was asked"
