"""Contracts for record validation and the reader's file diagnostics.

The writer's own round trip lives in
`tests/test_ci_compiler_cache_evidence.py`. These tests exercise the strict
validator and the location a malformed artefact is reported at.
"""

from __future__ import annotations

import json
import typing as typ

import pytest

from scripts import compiler_cache_records as records_reader
from scripts import compiler_cache_schema as schema
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
        ({"runner_class": "a b"}, "runner_class"),
        ({"restore_seconds_detail": "made-up"}, "restore_seconds_detail"),
        ({"job": "line one\nline two"}, "job"),
        ({"save_seconds": -1}, "save_seconds"),
        ({"restore_seconds": -0.5}, "restore_seconds"),
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


def test_a_json_null_line_fails_validation_instead_of_being_skipped(
    tmp_path: Path,
) -> None:
    """A non-blank `null` is an invalid record, not an empty line."""
    path = tmp_path / "records.jsonl"
    path.write_text("null\n", encoding="utf-8")
    with pytest.raises(records_reader.InvalidEvidenceError, match=r"records\.jsonl:1"):
        records_reader.load_records([path])


def test_the_reader_names_the_file_and_line_of_a_bad_record(tmp_path: Path) -> None:
    """A malformed artefact must be locatable among the downloaded files."""
    path = tmp_path / "records.jsonl"
    path.write_text(
        f"{json.dumps(record_fixture())}\n{json.dumps({'bracket': 'x'})}\n",
        encoding="utf-8",
    )
    with pytest.raises(records_reader.InvalidEvidenceError, match=r"records\.jsonl:2"):
        records_reader.load_records([path])
