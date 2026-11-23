"""Contracts for record validation and the summary's empty edge.

The writer's own round trip lives in
`tests/test_ci_compiler_cache_evidence.py`. These tests exercise the strict
validator and the report a reader writes when no evidence exists.
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
    from pathlib import Path


def test_a_valid_record_passes_validation() -> None:
    """The schema accepts what the writer produces."""
    validated = records_reader.validate_record(record_fixture())
    assert validated["bracket"] == "typecheck", "validation must preserve the record"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"schema_version": 2}, "schema version"),
        ({"record_kind": "other"}, "record kind"),
        ({"state": "healthy"}, "state"),
        ({"backend": "s3"}, "backend"),
        ({"zero_requests": "zero"}, "zero_requests"),
        ({"wrapper_present": 1}, "wrapper_present"),
        (
            {
                "counters": {
                    **dict.fromkeys(schema.COUNTERS, None),
                    "cache_hits": True,
                }
            },
            "cache_hits",
        ),
        ({"counters": {"cache_hits": 1}}, "counters"),
        ({"run_id": "one"}, "run_id"),
        ({"job": "line one\nline two"}, "job"),
        ({"extra": 1}, "exactly"),
    ],
)
def test_validation_rejects_an_out_of_contract_record(
    mutation: dict[str, object], expected: str
) -> None:
    """Every schema clause must reject its own violation."""
    with pytest.raises(records_reader.InvalidEvidenceError, match=expected):
        records_reader.validate_record(record_fixture(**mutation))


def test_the_reader_refuses_duplicate_brackets(tmp_path: Path) -> None:
    """Two records for one bracket in one attempt make the delta ambiguous."""
    path = tmp_path / "records.jsonl"
    line = json.dumps(record_fixture())
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")
    with pytest.raises(records_reader.InvalidEvidenceError, match="duplicates"):
        records_reader.load_records([path])


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
