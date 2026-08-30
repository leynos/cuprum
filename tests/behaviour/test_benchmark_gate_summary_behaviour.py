"""Behavioural tests that run the workflow's gate-summary script.

The contract tests next door assert that the step exists and mentions the
right things. That is not enough on its own: a script that wrote nothing, or
wrote the opposite verdict, would contain the same words and pass. These
tests extract the real `run:` block from `ci.yml`, execute it under `bash`
with the environment Actions would supply, and read back what it emitted.

The script only ever touches `$GITHUB_STEP_SUMMARY` and its own environment
variables, which is what makes running it outside Actions meaningful rather
than a simulation of it.
"""

from __future__ import annotations

import dataclasses as dc
import subprocess  # noqa: S404 - running the workflow's own script is the test
import typing as typ

import pytest
from pytest_bdd import given, parsers, scenario, then, when

from tests.helpers.workflow import CHANGES_JOB, script_of, step_named

if typ.TYPE_CHECKING:
    import pathlib as pth

FEATURE = "../features/benchmark_gate_summary.feature"
SUMMARY_STEP = "Record the benchmark gate decision"

#: Column order of the table the script emits.
_COLUMNS = ("event", "detector", "bench", "decision")


@dc.dataclass(frozen=True, slots=True)
class Detector:
    """What the paths-filter step reported to the summary step."""

    outcome: str
    bench: str


@dc.dataclass(frozen=True, slots=True)
class Summary:
    """The row the summary script emitted, by column."""

    fields: dict[str, str]


@scenario(FEATURE, "A pull request touching performance-relevant paths")
def test_a_pull_request_touching_performance_relevant_paths() -> None:
    """A pull request touching performance-relevant paths."""


@scenario(FEATURE, "A documentation-only pull request")
def test_a_documentation_only_pull_request() -> None:
    """A documentation-only pull request."""


@scenario(FEATURE, "A push to main is never gated")
def test_a_push_to_main_is_never_gated() -> None:
    """A push to main is never gated."""


@scenario(FEATURE, "The detector itself failed")
def test_the_detector_itself_failed() -> None:
    """The detector itself failed."""


def _summary_script() -> str:
    """Return the summary step's script, as `ci.yml` declares it."""
    script = script_of(step_named(CHANGES_JOB, SUMMARY_STEP))
    assert script is not None, f"the {SUMMARY_STEP!r} step must run a script"
    return script


def _run_summary_script(
    *, event: str, detector: Detector, tmp_path: pth.Path
) -> Summary:
    """Execute the real script and parse the row it appended."""
    summary_path = tmp_path / "step-summary.md"
    summary_path.touch()
    completed = subprocess.run(  # noqa: S603 - the checked-in workflow script is trusted
        ["/usr/bin/env", "bash", "-c", _summary_script()],
        env={
            "PATH": "/usr/bin:/bin",
            "EVENT": event,
            "BENCH": detector.bench,
            "DETECTOR": detector.outcome,
            "GITHUB_STEP_SUMMARY": str(summary_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"the summary script must not fail; stderr was:\n{completed.stderr}"
    )

    emitted = summary_path.read_text(encoding="utf-8")
    rows = [
        line
        for line in emitted.splitlines()
        if line.startswith("|") and not line.startswith("| ---")
    ]
    assert len(rows) == 2, (
        f"expected a header row and one data row; the script emitted:\n{emitted}"
    )
    values = [cell.strip() for cell in rows[1].strip("|").split("|")]
    assert len(values) == len(_COLUMNS), (
        f"expected {len(_COLUMNS)} columns, found {values} in:\n{emitted}"
    )
    return Summary(fields=dict(zip(_COLUMNS, values, strict=True)))


# -- Given steps ---------------------------------------------------------------


@given(
    parsers.parse(
        "the detector succeeded and reported {presence} performance-relevant changes"
    ),
    target_fixture="detector",
)
def given_the_detector_succeeded(presence: str) -> Detector:
    """Describe a detector run that completed and produced a verdict."""
    return Detector(outcome="success", bench="true" if presence != "no" else "false")


@given("the detector failed", target_fixture="detector")
def given_the_detector_failed() -> Detector:
    """Describe a detector run that failed, leaving its output unset."""
    return Detector(outcome="failure", bench="")


# -- When steps ----------------------------------------------------------------


@when(
    parsers.parse("the gate summary script runs for a {event} event"),
    target_fixture="summary",
)
def when_the_summary_script_runs(
    detector: Detector, event: str, tmp_path: pth.Path
) -> Summary:
    """Run the workflow's script for the stated event."""
    return _run_summary_script(event=event, detector=detector, tmp_path=tmp_path)


# -- Then steps ----------------------------------------------------------------


@then(parsers.parse("the summary records the benchmark job as {decision}"))
def then_the_summary_records(summary: Summary, decision: str) -> None:
    """Assert the recorded decision, which is the whole point of the step."""
    assert summary.fields["decision"] == decision, (
        f"expected the summary to record {decision!r}; it recorded {summary.fields!r}"
    )


@then(parsers.parse("the summary reports the detector as {outcome}"))
def then_the_summary_reports_the_detector(summary: Summary, outcome: str) -> None:
    """Assert the detector's own status reached the summary."""
    assert summary.fields["detector"] == outcome, (
        f"expected detector status {outcome!r}; summary was {summary.fields!r}"
    )


@then(parsers.parse("the summary reports the changed-path verdict as {verdict}"))
def then_the_summary_reports_the_verdict(summary: Summary, verdict: str) -> None:
    """Assert an absent verdict is named rather than defaulted to a value.

    Recording `false` when the detector never produced a verdict would read
    as "no performance-relevant changes", which is a claim nothing measured.
    """
    assert summary.fields["bench"] == verdict, (
        f"expected changed-path verdict {verdict!r}; summary was {summary.fields!r}"
    )


@pytest.mark.parametrize(
    ("event", "detector", "expected"),
    [
        pytest.param("pull_request", Detector("success", "true"), "run", id="relevant"),
        pytest.param(
            "pull_request", Detector("success", "false"), "skip", id="docs-only"
        ),
        pytest.param("push", Detector("success", "false"), "run", id="main-push"),
        pytest.param(
            "workflow_dispatch", Detector("success", "false"), "run", id="dispatch"
        ),
        pytest.param(
            "pull_request",
            Detector("failure", ""),
            "skip-detector-failed",
            id="detector-failed",
        ),
        pytest.param(
            "pull_request",
            Detector("", ""),
            "skip-detector-failed",
            id="detector-never-ran",
        ),
    ],
)
def test_the_recorded_decision_matches_the_gate(
    tmp_path: pth.Path, event: str, detector: Detector, expected: str
) -> None:
    """The recorded decision must match what the gate will actually do.

    The table is the whole input space the step has: two closed sets and an
    event name. Enumerating it keeps the summary from drifting away from the
    `if:` expression it describes, which is the failure a maintainer reading
    the summary could not detect.
    """
    summary = _run_summary_script(event=event, detector=detector, tmp_path=tmp_path)

    assert summary.fields["decision"] == expected, (
        f"expected decision {expected!r}; summary was {summary.fields!r}"
    )
    assert summary.fields["event"] == event, (
        f"expected event {event!r}; summary was {summary.fields!r}"
    )
