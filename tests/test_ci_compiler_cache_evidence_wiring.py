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


def _bracket_calls(workflow_name: str, job_name: str, bracket: str) -> dict[str, int]:
    """Return each evidence call's position for one bracket, by phase or record."""
    positions: dict[str, int] = {}
    for index, step in enumerate(steps(workflow_name, job_name)):
        declared = step.get("with")
        if not isinstance(declared, dict):
            continue
        inputs = step_inputs(step, f"{workflow_name}:{job_name} evidence inputs")
        if inputs.get("bracket") != bracket:
            continue
        mode = str(inputs.get("mode"))
        positions[str(inputs.get("phase")) if mode == "snapshot" else "record"] = index
    return positions


@pytest.mark.parametrize(("workflow_name", "job_name"), sorted(BRACKETS))
def test_every_bracket_snapshots_before_and_after_its_work(
    workflow_name: str, job_name: str
) -> None:
    """A bracket's two snapshots must surround the work they measure."""
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
        if step.get("uses") is not None:
            continue
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


def test_the_restore_timer_precedes_the_first_restore() -> None:
    """The restore window must include every cache download in the job."""
    job_steps = steps("ci.yml", "typecheck-test")
    marks = [
        index
        for index, step in enumerate(job_steps)
        if isinstance(step.get("with"), dict)
        and step_inputs(step, "mark inputs").get("marker") == "restore"
    ]
    restores = [
        index
        for index, step in enumerate(job_steps)
        if step.get("uses") == CACHE_RESTORE
    ]
    assert marks, "the job must mark the restore window"
    assert restores, "the job must restore a cache"
    assert min(marks) < min(restores), (
        f"the restore timer must precede the first restore, got marks {marks} "
        f"and restores {restores}"
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
