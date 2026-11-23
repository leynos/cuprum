"""Contracts for how the compiler-cache evidence steps are wired.

The writer and reader tests prove the record round trip. These tests prove the
workflow brackets the right steps and that evidence can never fail a build that
compiled correctly. They read the checked-in workflow and action, so a bracket
removed during an unrelated edit is a failing contract rather than a silently
thinner measurement.
"""

from __future__ import annotations

import itertools
import typing as typ

import pytest

from tests.helpers.ci_compiler_evidence import (
    BRACKETS,
    EVIDENCE_ACTION,
    EVIDENCE_ACTION_PATH,
)
from tests.helpers.ci_runners import CACHE_RESTORE, ROOT, step_inputs, steps
from tests.helpers.composite_actions import action_document

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.workflow_types import Step


def _bracket_calls(workflow_name: str, job_name: str, bracket: str) -> dict[str, int]:
    """Return each evidence call's position for one bracket, by phase or record."""
    return _positions_of(
        steps(workflow_name, job_name), bracket, workflow_name, job_name
    )


def _positions_of(
    job_steps: cabc.Sequence[Step],
    bracket: str,
    workflow_name: str,
    job_name: str,
) -> dict[str, int]:
    """Return each call's position, refusing a repeated phase."""
    positions: dict[str, int] = {}
    for index, step in enumerate(job_steps):
        declared = step.get("with")
        if not isinstance(declared, dict):
            continue
        inputs = step_inputs(step, f"{workflow_name}:{job_name} evidence inputs")
        if inputs.get("bracket") != bracket:
            continue
        mode = str(inputs.get("mode"))
        assert step.get("uses") == EVIDENCE_ACTION_PATH, (
            f"{workflow_name}:{job_name} must invoke the evidence action"
        )
        key = str(inputs.get("phase")) if mode == "snapshot" else "record"
        assert key not in positions, (
            f"{workflow_name}:{job_name} declares {key!r} for {bracket} twice"
        )
        positions[key] = index
    return positions


WORKLOAD_STEPS: typ.Final = {
    ("ci.yml", "lint-test", "lint"): (
        "Run lint, including Skylos dead-code detection",
        "Verify Rust MSRV",
        "Lint the Windows cfg branches",
    ),
    ("ci.yml", "typecheck-test", "typecheck"): ("Run typechecker",),
    ("ci.yml", "typecheck-test", "python-tests"): ("Run tests",),
    ("ci.yml", "extension-tests", "extension"): (
        "Build the native extension",
        "Run extension-gated tests",
        "Run typechecker",
    ),
}


@pytest.mark.parametrize(("workflow_name", "job_name"), sorted(BRACKETS))
def test_every_bracket_snapshots_before_and_after_its_work(
    workflow_name: str, job_name: str
) -> None:
    """A bracket's two snapshots must surround the work they measure."""
    names = [str(step.get("name", "")) for step in steps(workflow_name, job_name)]
    for bracket in BRACKETS[workflow_name, job_name]:
        positions = _bracket_calls(workflow_name, job_name, bracket)
        assert set(positions) == {"pre", "post", "record"}, (
            f"{workflow_name}:{job_name} must snapshot and record {bracket}, "
            f"found phases {sorted(positions)}"
        )
        assert positions["pre"] < positions["post"] < positions["record"], (
            f"{workflow_name}:{job_name} must order {bracket} as pre < post < "
            f"record, got {positions}"
        )
        for workload in WORKLOAD_STEPS[workflow_name, job_name, bracket]:
            index = names.index(workload)
            assert positions["pre"] < index < positions["post"], (
                f"{workflow_name}:{job_name} bracket {bracket} must enclose "
                f"{workload!r}, which sits at {index} outside "
                f"{(positions['pre'], positions['post'])}"
            )


def test_the_brackets_do_not_overlap() -> None:
    """Two brackets sharing a server must not interleave their deltas."""
    for (workflow_name, job_name), brackets in sorted(BRACKETS.items()):
        spans = [
            (
                bracket,
                _bracket_calls(workflow_name, job_name, bracket)["pre"],
                _bracket_calls(workflow_name, job_name, bracket)["post"],
            )
            for bracket in brackets
        ]
        for (first, _, first_post), (second, second_pre, _) in itertools.pairwise(
            spans
        ):
            assert first_post < second_pre, (
                f"{workflow_name}:{job_name} brackets {first} and {second} overlap"
            )


def test_no_evidence_step_can_fail_the_build() -> None:
    """Evidence issues must never gate a build that compiled correctly."""
    document = action_document(EVIDENCE_ACTION)
    runs = typ.cast("dict[str, object]", document["runs"])
    for step in typ.cast("list[dict[str, object]]", runs["steps"]):
        name = step.get("name")
        assert step.get("continue-on-error") is True, (
            f"the {name!r} step must be continue-on-error"
        )


def test_the_evidence_action_uploads_with_bounded_retention() -> None:
    """Keep the artefact small and short-lived, as the telemetry precedent does."""
    document = action_document(EVIDENCE_ACTION)
    runs = typ.cast("dict[str, object]", document["runs"])
    uploads = [
        step
        for step in typ.cast("list[dict[str, object]]", runs["steps"])
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1, "the action must upload exactly once"
    declared = typ.cast("dict[str, object]", uploads[0]["with"])
    assert typ.cast("int", declared["retention-days"]) <= 90, (
        f"retention must not exceed the 90-day convention, got {declared}"
    )
    assert "records.jsonl" in str(declared["path"]), (
        "the action must upload the records"
    )


def test_the_action_invokes_the_checked_in_writer() -> None:
    """The composite action names a path; the script must exist there."""
    document = action_document(EVIDENCE_ACTION)
    runs = typ.cast("dict[str, object]", document["runs"])
    joined = " ".join(
        str(step.get("run", ""))
        for step in typ.cast("list[dict[str, object]]", runs["steps"])
    )
    assert "scripts/compiler_cache_evidence.py" in joined, (
        "the action must invoke the checked-in writer"
    )
    assert (ROOT / "scripts" / "compiler_cache_evidence.py").is_file(), (
        "scripts/compiler_cache_evidence.py must exist"
    )


def test_each_restore_timer_immediately_precedes_the_compiler_restore() -> None:
    """The restore span must measure the transfer, not the setup before it.

    A marker placed before the registry and tool restores would charge their
    download time to the compiler cache, which a migration does not remove.
    The marker therefore sits immediately before the job's compiler restore.
    """
    for workflow_name, job_name, _bracket in WORKLOAD_STEPS:
        job_steps = steps(workflow_name, job_name)
        marks = [
            index
            for index, step in enumerate(job_steps)
            if isinstance(step.get("with"), dict)
            and step_inputs(step, "mark inputs").get("marker") == "restore"
        ]
        compiler_restores = [
            index
            for index, step in enumerate(job_steps)
            if step.get("uses") == CACHE_RESTORE
            and "~/.cache/sccache"
            in str(typ.cast("dict[str, object]", step.get("with", {})).get("path", ""))
        ]
        ends = [
            index
            for index, step in enumerate(job_steps)
            if isinstance(step.get("with"), dict)
            and step_inputs(step, "mark inputs").get("marker") == "restore-end"
        ]
        assert len(marks) == 1, f"{workflow_name}:{job_name} must mark one restore"
        assert len(ends) == 1, f"{workflow_name}:{job_name} must end the restore timer"
        assert len(compiler_restores) == 1, (
            f"{workflow_name}:{job_name} must restore the compiler cache once"
        )
        assert compiler_restores[0] == marks[0] + 1, (
            f"{workflow_name}:{job_name} must mark the restore immediately "
            f"before the compiler cache, got mark {marks[0]} and restore "
            f"{compiler_restores[0]}"
        )
        assert ends[0] == compiler_restores[0] + 1, (
            f"{workflow_name}:{job_name} must end the restore timer immediately "
            f"after the compiler cache, got restore {compiler_restores[0]} and "
            f"end {ends[0]}"
        )


def test_only_the_three_candidate_jobs_are_instrumented() -> None:
    """Leave the ratchet, proof lanes, Windows and macOS out of the experiment."""
    instrumented = {job for _workflow, job in BRACKETS}
    assert instrumented == {"lint-test", "typecheck-test", "extension-tests"}, (
        f"the experiment instruments three jobs, got {sorted(instrumented)}"
    )
    for workflow_name, job_name in (
        ("ci.yml", "benchmark-ratchet"),
        ("ci.yml", "extension-tests-windows"),
        ("ci.yml", "modern-stable"),
        ("loom.yml", "loom"),
        ("rust-boundaries.yml", "native"),
        ("coverage-main.yml", "coverage-upload"),
    ):
        calls = [
            step
            for step in steps(workflow_name, job_name)
            if step.get("uses") == EVIDENCE_ACTION_PATH
        ]
        assert not calls, f"{workflow_name}:{job_name} must not carry evidence steps"


def test_every_record_call_receives_the_restore_result() -> None:
    """A record with no restore result can never count as a warm control."""
    for workflow_name, job_name, _bracket in WORKLOAD_STEPS:
        for step in steps(workflow_name, job_name):
            if step.get("uses") != EVIDENCE_ACTION_PATH:
                continue
            inputs = step_inputs(step, f"{workflow_name}:{job_name} evidence inputs")
            if inputs.get("mode") != "record":
                continue
            assert "matched-key" in inputs, (
                f"{workflow_name}:{job_name} record must pass the matched key"
            )
            assert "restore-hit" in inputs, (
                f"{workflow_name}:{job_name} record must pass the restore result"
            )
            assert "cache-matched-key" in str(inputs["matched-key"]), (
                "the matched key must come from the restore output, not cache-hit"
            )
            assert "cache-matched-key" in str(inputs["restore-hit"]), (
                "the rolling key never exact-matches; use cache-matched-key"
            )


def test_the_upload_name_carries_the_matrix_leg() -> None:
    """Two matrix legs of one job must not collide on one artefact name."""
    document = action_document(EVIDENCE_ACTION)
    runs = typ.cast("dict[str, object]", document["runs"])
    uploads = [
        step
        for step in typ.cast("list[dict[str, object]]", runs["steps"])
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    name = str(typ.cast("dict[str, object]", uploads[0]["with"])["name"])
    assert "inputs.matrix-leg" in name, (
        f"the artefact name must include the matrix leg, got {name!r}"
    )


def test_a_duplicate_record_call_is_rejected() -> None:
    """Two records for one bracket would be refused by the reader."""
    duplicate = [
        {
            "uses": EVIDENCE_ACTION_PATH,
            "with": {"mode": "snapshot", "bracket": "x", "phase": "pre"},
        },
        {
            "uses": EVIDENCE_ACTION_PATH,
            "with": {"mode": "snapshot", "bracket": "x", "phase": "post"},
        },
        {"uses": EVIDENCE_ACTION_PATH, "with": {"mode": "record", "bracket": "x"}},
        {"uses": EVIDENCE_ACTION_PATH, "with": {"mode": "record", "bracket": "x"}},
    ]
    with pytest.raises(AssertionError, match="twice"):
        _positions_of(typ.cast("list[Step]", duplicate), "x", "ci.yml", "job")
