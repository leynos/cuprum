"""The release telemetry record, its closed vocabularies, and its wiring.

``scripts/release_telemetry.py`` writes one JSONL record per release phase,
following the benchmark-gate precedent (ADR-014). These tests pin the schema,
prove every label stays inside its vocabulary whatever the environment holds,
check the retry and HTTP-status reporting, and read ``release.yml`` and the
``release-telemetry`` action to show a record is still written and uploaded
when the phase it describes has failed.
"""

from __future__ import annotations

import json
import re
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts import release_telemetry as telemetry
from tests.helpers.ci_workflows import ROOT, jobs
from tests.helpers.release_workflow import WORKFLOW, outputs, run_bash
from tests.helpers.strict_yaml import load

if typ.TYPE_CHECKING:
    import pathlib as pth

_NOW = 1_800_000_000.0
_VOCABULARIES: typ.Final = {
    "operation": telemetry.OPERATIONS,
    "outcome": telemetry.OUTCOMES,
    "failure_category": telemetry.FAILURE_CATEGORIES,
    "retry_bucket": frozenset(telemetry.RETRY_BUCKETS),
    "elapsed_bucket": frozenset(telemetry.ELAPSED_BUCKETS),
    "http_status_class": telemetry.HTTP_STATUS_CLASSES,
}
_ACTION = "./.github/actions/release-telemetry"
_OUTCOME_REFERENCE = re.compile(r"(\w+)=\$\{\{ steps\.([\w-]+)\.outcome \}\}")
_WARNING = "::warning title=release-telemetry::Invalid record fields; record omitted.\n"


def _environ(tmp_path: pth.Path, **overrides: str) -> dict[str, str]:
    """Return a valid writer environment with ``overrides`` applied."""
    return {
        "RELEASE_OPERATION": "publish_pypi",
        "RELEASE_STEPS": "index_http=success digest_mismatch=success upload=success",
        "RELEASE_JOB_STATUS": "success",
        "RELEASE_ATTEMPTS": "1",
        "RELEASE_HTTP_STATUS": "200",
        "PHASE_STARTED_AT": str(int(_NOW) - 30),
        "GITHUB_RUN_ID": "123456789",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_REF_NAME": "v0.2.0-beta1",
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        **overrides,
    }


def _records(tmp_path: pth.Path) -> list[dict[str, object]]:
    """Return every record the writer persisted under ``tmp_path``."""
    path = tmp_path / "runner-temp" / "release-telemetry" / "records.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_record_has_exactly_the_versioned_schema(tmp_path: pth.Path) -> None:
    """Identity, tag, and time are metadata; everything else is a label."""
    assert telemetry.main(_environ(tmp_path), _NOW) == 0

    [record] = _records(tmp_path)
    assert record == {
        "schema_version": 1,
        "metric": "release_phase_outcomes_total",
        "value": 1,
        "labels": {
            "operation": "publish_pypi",
            "outcome": "success",
            "failure_category": "none",
            "retry_bucket": "0",
            "elapsed_bucket": "under_1m",
            "http_status_class": "2xx",
        },
        "run_id": "123456789",
        "run_attempt": "2",
        "tag": "v0.2.0-beta1",
        "recorded_at": "2027-01-15T08:00:00Z",
    }
    assert type(record["value"]) is int, "a boolean must not pass for the count"
    assert outputs(tmp_path / "github-output") == {"written": "true"}


def test_a_job_writing_twice_keeps_both_records(tmp_path: pth.Path) -> None:
    """``publish-release`` records two phases into its one artefact."""
    telemetry.main(_environ(tmp_path, RELEASE_OPERATION="github_upload"), _NOW)
    telemetry.main(_environ(tmp_path, RELEASE_OPERATION="publish_release"), _NOW)

    operations = [
        typ.cast("dict[str, str]", record["labels"])["operation"]
        for record in _records(tmp_path)
    ]
    assert operations == ["github_upload", "publish_release"]


@pytest.mark.parametrize(
    ("steps", "job_status", "expected"),
    [
        ("index_http=success upload=success", "success", ("success", "none")),
        ("index_http=success upload=skipped", "success", ("skipped", "none")),
        ("index_http=failure upload=skipped", "failure", ("failure", "index_http")),
        ("github_api=success upload=failure", "failure", ("failure", "upload")),
        ("index_http=skipped upload=skipped", "failure", ("failure", "setup")),
    ],
    ids=["success", "nothing-to-do", "first-failure", "action-failure", "setup"],
)
def test_the_outcome_names_the_first_failed_step(
    steps: str, job_status: str, expected: tuple[str, str]
) -> None:
    """A failure is attributed to the step that failed, or to setup."""
    assert telemetry.outcome_of(steps, job_status) == expected


@pytest.mark.parametrize(
    ("attempts", "bucket"),
    [("", "0"), ("0", "0"), ("1", "0"), ("2", "1-2"), ("3", "1-2"), ("6", "3+")],
)
def test_retries_are_reported_in_closed_bands(attempts: str, bucket: str) -> None:
    """Attempts beyond the first are retries, banded 0, 1-2, and 3+."""
    assert telemetry.retry_bucket(attempts) == bucket


@pytest.mark.parametrize(
    ("status", "status_class"),
    [("200", "2xx"), ("404", "4xx"), ("503", "5xx"), ("000", "network"), ("", "none")],
)
def test_the_index_status_is_reported_by_class(status: str, status_class: str) -> None:
    """Only the class of the index's HTTP status is recorded."""
    assert telemetry.http_status_class(status) == status_class


@pytest.mark.parametrize(
    ("elapsed", "bucket"),
    [
        (0, "under_1m"),
        (59, "under_1m"),
        (60, "1m_5m"),
        (899, "5m_15m"),
        (900, "over_15m"),
    ],
)
def test_elapsed_time_falls_in_closed_bands(elapsed: int, bucket: str) -> None:
    """Each band's upper bound is exclusive."""
    assert telemetry.elapsed_bucket(str(int(_NOW) - elapsed), _NOW) == bucket


@pytest.mark.parametrize(
    "overrides",
    [
        {"RELEASE_OPERATION": "deploy"},
        {"RELEASE_STEPS": "timeout=failure"},
        {"RELEASE_STEPS": ""},
        {"GITHUB_RUN_ID": "12a"},
        {"GITHUB_REF_NAME": "main"},
        {"GITHUB_REF_NAME": "v1.2.3;rm -rf"},
    ],
    ids=["operation", "category", "no-steps", "run-id", "branch", "tag-payload"],
)
def test_an_invalid_field_omits_the_record_without_failing(
    tmp_path: pth.Path, overrides: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused record leaves one fixed warning and never fails the phase."""
    assert telemetry.main(_environ(tmp_path, **overrides), _NOW) == 0

    assert _records(tmp_path) == []
    assert capsys.readouterr().out == _WARNING, "the warning must not echo input"


def test_an_unwritable_record_warns_without_failing(
    tmp_path: pth.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A storage failure is reported and swallowed."""
    blocked = tmp_path / "runner-temp"
    blocked.write_text("not a directory", encoding="utf-8")

    assert telemetry.main(_environ(tmp_path), _NOW) == 0
    assert "Could not persist the release record." in capsys.readouterr().out


_ANY_TEXT = st.text(max_size=12)
_STEP_PAIRS = st.lists(
    st.tuples(st.sampled_from([*telemetry.FAILURE_CATEGORIES, "bogus"]), _ANY_TEXT),
    max_size=4,
).map(lambda pairs: " ".join(f"{name}={result}" for name, result in pairs))
_ENVIRONMENTS = st.fixed_dictionaries({
    "RELEASE_OPERATION": st.sampled_from([*telemetry.OPERATIONS, "deploy"]),
    "RELEASE_STEPS": _STEP_PAIRS,
    "RELEASE_JOB_STATUS": _ANY_TEXT,
    "RELEASE_ATTEMPTS": _ANY_TEXT,
    "RELEASE_HTTP_STATUS": _ANY_TEXT,
    "PHASE_STARTED_AT": _ANY_TEXT,
    "GITHUB_RUN_ID": st.just("1"),
    "GITHUB_RUN_ATTEMPT": st.just("1"),
    "GITHUB_REF_NAME": st.just("v1.2.3"),
})


@given(environ=_ENVIRONMENTS)
def test_every_label_stays_inside_its_vocabulary(environ: dict[str, str]) -> None:
    """Whatever the environment holds, a label is closed or the record omitted."""
    try:
        record = telemetry.build_record(environ, _NOW)
    except ValueError:
        return
    labels = typ.cast("dict[str, str]", record["labels"])
    assert set(labels) == set(_VOCABULARIES)
    for name, value in labels.items():
        assert value in _VOCABULARIES[name], f"{name}={value!r} is unbounded"


def _telemetry_steps(job_name: str) -> list[dict[str, typ.Any]]:
    """Return one job's telemetry steps, in order."""
    job = typ.cast("dict[str, typ.Any]", jobs(WORKFLOW)[job_name])
    return [item for item in job["steps"] if item.get("uses") == _ACTION]


def _assert_outcome_references_are_known(
    job_name: str, declared_steps: str, ids: set[object]
) -> None:
    """Assert every ``category=outcome`` reference names a real, closed pair."""
    assert not _OUTCOME_REFERENCE.sub("", declared_steps).strip(), (
        f"{job_name} must list only category=outcome pairs"
    )
    for category, step_id in _OUTCOME_REFERENCE.findall(declared_steps):
        assert category in telemetry.FAILURE_CATEGORIES, (
            f"{job_name} references unknown failure category {category!r}"
        )
        assert step_id in ids, f"{job_name} has no step {step_id!r}"


def _assert_records_on_every_outcome(
    job_name: str, item: dict[str, typ.Any], ids: set[object]
) -> str:
    """Assert one telemetry step fails open and reports a real operation."""
    assert item["if"] == "${{ !cancelled() }}", "a failed phase must record"
    assert item["continue-on-error"] is True, "telemetry must fail open"
    assert item["with"]["job-status"] == "${{ job.status }}", (
        f"{job_name} must report the job's own status"
    )
    _assert_outcome_references_are_known(job_name, item["with"]["steps"], ids)
    return typ.cast("str", item["with"]["operation"])


def test_every_release_phase_records_even_when_it_fails() -> None:
    """Each phase is recorded once, after its steps, unless the run is cancelled."""
    recorded = []
    for job_name, job in jobs(WORKFLOW).items():
        declared = typ.cast("dict[str, typ.Any]", job)
        if "steps" not in declared:
            continue
        ids = {item.get("id") for item in declared["steps"]}
        found = _telemetry_steps(job_name)
        assert found, f"{job_name} must record its phase"
        assert declared["steps"][-1] == found[-1], "the record must come last"
        assert found[-1]["with"].get("upload", "true") == "true"
        assert all(item["with"]["upload"] == "false" for item in found[:-1])
        recorded.extend(
            _assert_records_on_every_outcome(job_name, item, ids) for item in found
        )
    assert sorted(recorded) == sorted(telemetry.OPERATIONS)


def _action() -> dict[str, typ.Any]:
    """Parse the release-telemetry composite action."""
    path = ROOT / ".github" / "actions" / "release-telemetry" / "action.yml"
    return typ.cast("dict[str, typ.Any]", load(path.read_text("utf-8"), path.name))


def test_the_action_writes_fail_open_and_uploads_for_ninety_days() -> None:
    """The record is kept per job and attempt, and nothing it does gates."""
    write, upload, warn = _action()["runs"]["steps"]
    assert write["continue-on-error"] is True
    assert upload["continue-on-error"] is True
    assert upload["if"] == "${{ !cancelled() && inputs.upload == 'true' }}"
    assert upload["with"] == {
        "name": "release-telemetry-${{ github.job }}-${{ github.run_attempt }}",
        "path": "${{ runner.temp }}/release-telemetry/records.jsonl",
        "retention-days": 90,
        "if-no-files-found": "warn",
    }
    assert "steps.write.outcome == 'failure'" in warn["if"]


def test_the_action_records_a_failed_phase(tmp_path: pth.Path) -> None:
    """The action's own script persists a failure record."""
    write = _action()["runs"]["steps"][0]
    environ = _environ(
        tmp_path,
        GITHUB_WORKSPACE=str(ROOT),
        RELEASE_STEPS="index_http=success digest_mismatch=failure upload=skipped",
        RELEASE_JOB_STATUS="failure",
    )

    completed = run_bash(write["run"], tmp_path, environ)

    assert completed.returncode == 0, completed.stderr
    [record] = _records(tmp_path)
    labels = typ.cast("dict[str, str]", record["labels"])
    assert (labels["outcome"], labels["failure_category"]) == (
        "failure",
        "digest_mismatch",
    )
