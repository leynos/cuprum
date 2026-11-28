"""Contracts for the jobs loader and the report's job join.

The summary's whole-job figures exist only if the jobs document is read and
joined correctly. These tests exercise the loader's accepted shapes and the
report's `--jobs` path, so an ignored document cannot leave every total
unknown without failing a contract.
"""

from __future__ import annotations

import json
import typing as typ

import pytest

from scripts import compiler_cache_records as records_reader
from scripts import compiler_cache_report as report
from tests.helpers.ci_compiler_evidence import record_fixture

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_a_missing_jobs_path_loads_nothing(tmp_path: Path) -> None:
    """An absent `--jobs` argument is an empty join, not a failure."""
    assert not records_reader.load_jobs(None), "a missing path must load nothing"


def test_one_response_and_a_list_of_responses_agree(tmp_path: Path) -> None:
    """The loader accepts either one API response or a list of them."""
    job = {"run_id": 1, "run_attempt": 1, "name": "typecheck-test"}
    single = tmp_path / "single.json"
    single.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    listed = tmp_path / "listed.json"
    listed.write_text(json.dumps([{"jobs": [job]}]), encoding="utf-8")
    assert records_reader.load_jobs(single) == [job], "one response must load its job"
    assert records_reader.load_jobs(listed) == [job], "a list must load its job"


def test_a_response_without_a_jobs_list_is_rejected(tmp_path: Path) -> None:
    """A malformed document must not flatten into an empty join."""
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"items": []}), encoding="utf-8")
    with pytest.raises(records_reader.InvalidEvidenceError, match="jobs list"):
        records_reader.load_jobs(path)


def test_non_dictionary_jobs_are_skipped(tmp_path: Path) -> None:
    """A stray scalar in the list is ignored; a job object is kept."""
    job = {"run_id": 1, "run_attempt": 1, "name": "typecheck-test"}
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"jobs": [job, 3, "x"]}), encoding="utf-8")
    assert records_reader.load_jobs(path) == [job], "only dictionary entries are jobs"


def test_the_report_joins_a_jobs_file_into_whole_job_seconds(tmp_path: Path) -> None:
    """A real `--jobs` file must change the paid total, not be ignored."""
    records_dir = tmp_path / "records"
    records_dir.mkdir()
    (records_dir / "records.jsonl").write_text(
        json.dumps(record_fixture()) + "\n", encoding="utf-8"
    )
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps({
            "jobs": [
                {
                    "run_id": 1,
                    "run_attempt": 1,
                    "name": "typecheck-test",
                    "started_at": "2026-11-22T00:00:00Z",
                    "completed_at": "2026-11-22T00:01:40Z",
                    "conclusion": "success",
                }
            ]
        }),
        encoding="utf-8",
    )
    names_path = tmp_path / "names.json"
    names_path.write_text(
        json.dumps({"typecheck-test": "typecheck-test"}), encoding="utf-8"
    )
    output = tmp_path / "summary.json"
    status = report.main([
        "--records",
        str(records_dir),
        "--jobs",
        str(jobs_path),
        "--job-names",
        str(names_path),
        "--out",
        str(output),
    ])
    assert status == 0, f"the report must succeed, got {status}"
    summary = json.loads(output.read_text(encoding="utf-8"))
    cost = summary["cost"]
    assert cost["paid_job_seconds_by_workflow"] == {"ci.yml": 100.0}, (
        f"the jobs file must price the joined job, got {cost}"
    )
