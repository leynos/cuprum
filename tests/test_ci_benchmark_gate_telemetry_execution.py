"""Exercise the workflow log writer and inspect its persisted JSON records."""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import json
import typing as typ

import pytest

from tests.helpers.benchmark_gate_telemetry import (
    IDENTITY_KEYS,
    LABEL_NAMES,
    VALID_IDENTITY,
    Verdict,
    run_log_script,
)
from tests.helpers.workflow import mapping

if typ.TYPE_CHECKING:
    import pathlib as pth

    from tests.helpers.workflow import Workflow

RECORDED_RUN = Verdict("pull_request", "success", "run")
VOCABULARIES = {
    "event_class": {"pull_request", "other"},
    "detector_status": {"success", "failure", "unknown"},
    "decision": {"run", "skip", "skip-detector-failed"},
}

#: The complete refusal diagnostic. Pinning the whole line is what makes
#: "bounded" checkable: the text is fixed, so it cannot grow with the value it
#: refuses and cannot carry that value out of the writer.
_REFUSAL_WARNING = (
    "::warning title=benchmark-gate-log::Invalid record fields; log omitted.\n"
)

#: Non-ASCII decimal digits. `str.isdecimal` accepts these and `str.isascii`
#: does not, so they are what separates the two halves of the writer's
#: `isascii() and isdecimal()` test. Arabic-Indic digits are used rather than a
#: lookalike, because Unicode classes them as decimal digits in their own right.
_NON_ASCII_DIGITS = "١٢٣"


def _identity_name(key: str) -> str:
    """Return a run-identity variable's name as it reads in a test id."""
    return key.removeprefix("GITHUB_").lower().replace("_", "-")


def _identity_without(key: str) -> dict[str, str]:
    """Return the accepted identity with one variable removed entirely."""
    return {name: value for name, value in VALID_IDENTITY.items() if name != key}


def _identity_with(key: str, value: str) -> dict[str, str]:
    """Return the accepted identity with one variable replaced."""
    return {**VALID_IDENTITY, key: value}


#: Values a writer that checks the identity must refuse. Each is one dimension
#: of the check: an empty value, a padded one, digits that are decimal but not
#: ASCII, and text that is ASCII but not decimal. A writer that dropped either
#: half of `isascii() and isdecimal()`, or that started calling `strip()`, would
#: accept one of these and fail on the row naming it.
_REFUSED_VALUES = (
    ("empty", ""),
    ("padded", " 123456789"),
    ("non-ascii", _NON_ASCII_DIGITS),
    ("non-decimal", "12345678a"),
)

#: Identities the writer must refuse, one row per key per dimension. Absence is
#: generated from the keys the helper injects, so adding a third identity
#: variable to `VALID_IDENTITY` cannot leave the refusal path untested.
_REFUSED_IDENTITIES = [
    *(
        pytest.param(_identity_without(key), id=f"{_identity_name(key)}-missing")
        for key in IDENTITY_KEYS
    ),
    *(
        pytest.param(_identity_with(key, value), id=f"{_identity_name(key)}-{label}")
        for key in IDENTITY_KEYS
        for label, value in _REFUSED_VALUES
    ),
]


@pytest.mark.parametrize(
    "verdict",
    [
        Verdict(event_class, detector_status, decision)
        for event_class in sorted(VOCABULARIES["event_class"])
        for detector_status in sorted(VOCABULARIES["detector_status"])
        for decision in sorted(VOCABULARIES["decision"])
    ],
)
def test_closed_label_combinations_are_stored_verbatim(
    tmp_path: pth.Path,
    workflow_data: Workflow,
    *,
    verdict: Verdict,
) -> None:
    """Every permitted label combination remains unchanged in the real file."""
    run = run_log_script(
        verdict=verdict, workflow_data=workflow_data, tmp_path=tmp_path
    )
    assert run.exit_code == 0, f"log writer failed: {run.stderr}"
    assert len(run.body.splitlines()) == 1, "one execution must persist one JSON line"
    decoded: object = json.loads(run.body)
    record = mapping(decoded, "decision record must be a mapping")
    assert set(record) == {
        "schema_version",
        "metric",
        "value",
        "labels",
        "run_id",
        "run_attempt",
        "recorded_at",
    }, "the record must expose only the documented schema"
    assert record["schema_version"] == 1, "record schema must be explicitly versioned"
    assert record["metric"] == "benchmark_gate_decisions_total", (
        "metric name must stay stable"
    )
    assert record["value"] == 1, "each record represents exactly one gate observation"
    labels = mapping(record["labels"], "metric labels must be a mapping")
    assert set(labels) == set(LABEL_NAMES), "metadata must never become metric labels"
    assert labels == verdict.as_labels(), "stored labels must reuse the gate outputs"
    assert record["run_id"] == "123456789", "run ID must remain separate metadata"
    assert record["run_attempt"] == "2", "attempt identity supports deduplication"
    timestamp = record["recorded_at"]
    assert isinstance(timestamp, str), "record time must be an ISO timestamp"
    assert timestamp.endswith("Z"), "record time must explicitly use UTC"
    assert dt.datetime.fromisoformat(timestamp).tzinfo is not None, (
        "record time must be aware"
    )
    assert run.body.endswith("\n"), "records must concatenate as valid JSON Lines"
    assert run.outputs == "written=true\nrecord=" + run.body, (
        "outputs must match the persisted file"
    )


@pytest.mark.parametrize("label", tuple(VOCABULARIES))
@pytest.mark.parametrize("value", ["", "docs/private.txt", '"}\nprivate-command'])
def test_invalid_labels_are_refused_without_leaking_them(
    tmp_path: pth.Path, workflow_data: Workflow, *, label: str, value: str
) -> None:
    """Unbounded inputs must never enter records, outputs, or diagnostics."""
    run = run_log_script(
        verdict=dc.replace(RECORDED_RUN, **{label: value}),
        workflow_data=workflow_data,
        tmp_path=tmp_path,
    )
    assert run.exit_code == 0, "invalid logging input must not fail CI"
    assert not run.body, "invalid input must not produce an artefact"
    assert not run.outputs, "invalid input must not advertise a record"
    assert "::warning" in run.stdout, "omitted logs need a visible warning"
    if value:
        assert value not in run.stdout + run.stderr, (
            "diagnostics must not echo invalid data"
        )


@pytest.mark.parametrize("identity", _REFUSED_IDENTITIES)
def test_invalid_run_identities_are_refused_without_leaking_them(
    tmp_path: pth.Path, workflow_data: Workflow, *, identity: dict[str, str]
) -> None:
    """Refuse a run identity the record cannot carry, and stay fail-open.

    `GITHUB_RUN_ID` and `GITHUB_RUN_ATTEMPT` name the run a record belongs to.
    Nothing in the other cases would notice if the writer stopped checking them:
    the identity is only read through these two variables, so deleting the check
    leaves every other test green while the stored records start claiming `run_id
    ""`. The claim is not merely wrong — a JSONL consumer grouping by run cannot
    tell an unset identity from a real one, and the values are free-form text
    that reaches the same file as the metric labels.

    Fail-open is asserted alongside the refusal, because the tempting way to
    enforce the check is to let it raise, which would turn a malformed runner
    environment into a failed CI job rather than an omitted record.
    """
    run = run_log_script(
        verdict=RECORDED_RUN,
        workflow_data=workflow_data,
        tmp_path=tmp_path,
        identity=identity,
    )
    assert run.exit_code == 0, "an invalid identity must not fail CI"
    assert not run.body, "invalid input must not produce an artefact"
    assert not run.outputs, "invalid input must not advertise a record"
    assert run.stdout == _REFUSAL_WARNING, (
        "the refusal must be exactly the bounded diagnostic; a longer one could "
        f"carry the refused value out of the writer; found {run.stdout!r}"
    )
    refused = {value for value in identity.values() if value}
    for value in refused - set(VALID_IDENTITY.values()):
        assert value not in run.stdout + run.stderr, (
            "diagnostics must not echo invalid data"
        )


def test_storage_failure_does_not_fail_the_gate(
    tmp_path: pth.Path, workflow_data: Workflow
) -> None:
    """A blocked output directory must warn without affecting admission."""
    (tmp_path / "benchmark-gate").write_text("not a directory", encoding="utf-8")
    run = run_log_script(
        verdict=RECORDED_RUN, workflow_data=workflow_data, tmp_path=tmp_path
    )
    assert run.exit_code == 0, "local storage failure must remain fail-open"
    assert not run.body, "failed writes must not leave a record"
    assert not run.outputs, "failed writes must not advertise a record"
    assert "::warning" in run.stdout, "storage failures need a visible warning"
