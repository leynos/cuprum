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

import typing as typ

import pytest
from pytest_bdd import given, parsers, scenario, then, when

from tests.behaviour.test_benchmark_gate_summary_support import (
    Detector,
    Summary,
    SummaryCase,
    run_summary_script,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion

    from tests.helpers.workflow import Workflow

FEATURE = "../features/benchmark_gate_summary.feature"


@scenario(FEATURE, "A pull request touching performance-relevant paths")
def test_a_pull_request_touching_performance_relevant_paths() -> None:
    """Record the run decision for a performance-relevant pull request.

    Notes
    -----
    The feature scenario expects the benchmark job to run after successful
    detection reports performance-relevant changes.
    """


@scenario(FEATURE, "A documentation-only pull request")
def test_a_documentation_only_pull_request() -> None:
    """Record the skip decision for a documentation-only pull request.

    Notes
    -----
    The feature scenario expects the benchmark job to skip after successful
    detection reports no performance-relevant changes.
    """


@scenario(FEATURE, "A push to main is never gated")
def test_a_push_to_main_is_never_gated() -> None:
    """Record the run decision for an ungated push to ``main``.

    Notes
    -----
    The feature scenario expects the benchmark job to run regardless of a
    successful detector result that reports no performance-relevant changes.
    """


@scenario(FEATURE, "The detector itself failed")
def test_the_detector_itself_failed() -> None:
    """Record the failed-detector decision for a pull request.

    Notes
    -----
    The feature scenario expects the benchmark job to skip with the
    ``skip-detector-failed`` decision when path detection fails.
    """


@scenario(FEATURE, "The detector fails for a push")
def test_the_detector_fails_for_a_push() -> None:
    """Record that a failed detector skips a non-pull-request event."""


# -- Given steps ---------------------------------------------------------------


@given(
    parsers.parse(
        "the detector succeeded and reported {presence} performance-relevant changes"
    ),
    target_fixture="detector",
)
def given_the_detector_succeeded(presence: str) -> Detector:
    """Describe a detector run that completed and produced a verdict.

    Parameters
    ----------
    presence : str
        Feature-file value indicating whether relevant paths were found.

    Returns
    -------
    Detector
        Detector result with a successful outcome and Boolean path verdict.
    """
    return Detector(outcome="success", bench="true" if presence != "no" else "false")


@given("the detector failed", target_fixture="detector")
def given_the_detector_failed() -> Detector:
    """Describe a detector run that failed, leaving its output unset.

    Returns
    -------
    Detector
        Detector result with failure status and no path verdict.
    """
    return Detector(outcome="failure", bench="")


# -- When steps ----------------------------------------------------------------


@when(
    parsers.parse("the gate summary script runs for a {event} event"),
    target_fixture="summary",
)
def when_the_summary_script_runs(
    detector: Detector, event: str, tmp_path: pth.Path, workflow_data: Workflow
) -> Summary:
    """Run the workflow's summary script for the stated event.

    Parameters
    ----------
    detector : Detector
        Detector status and path verdict to expose to the workflow script.
    event : str
        Event class supplied through the workflow environment.
    tmp_path : pathlib.Path
        Pytest temporary directory in which to capture the step summary.

    Returns
    -------
    Summary
        Parsed summary row emitted by the workflow script.
    """
    return run_summary_script(
        event=event,
        detector=detector,
        tmp_path=tmp_path,
        workflow_data=workflow_data,
    )


# -- Then steps ----------------------------------------------------------------


@then(parsers.parse("the summary records the benchmark job as {decision}"))
def then_the_summary_records(summary: Summary, decision: str) -> None:
    """Assert the benchmark decision recorded in the summary.

    Parameters
    ----------
    summary : Summary
        Parsed row emitted by the workflow summary script.
    decision : str
        Expected benchmark decision from the feature scenario.

    """
    assert summary.fields["decision"] == decision, (
        f"expected the summary to record {decision!r}; it recorded {summary.fields!r}"
    )


@then(parsers.parse("the summary reports the detector as {outcome}"))
def then_the_summary_reports_the_detector(summary: Summary, outcome: str) -> None:
    """Assert that the detector status reached the summary.

    Parameters
    ----------
    summary : Summary
        Parsed row emitted by the workflow summary script.
    outcome : str
        Expected detector outcome from the feature scenario.

    """
    assert summary.fields["detector"] == outcome, (
        f"expected detector status {outcome!r}; summary was {summary.fields!r}"
    )


@then(parsers.parse("the summary reports the changed-path verdict as {verdict}"))
def then_the_summary_reports_the_verdict(summary: Summary, verdict: str) -> None:
    """Assert an absent verdict is named rather than defaulted to a value.

    Recording `false` when the detector never produced a verdict would read
    as "no performance-relevant changes", which is a claim nothing measured.

    Parameters
    ----------
    summary : Summary
        Parsed row emitted by the workflow summary script.
    verdict : str
        Expected changed-path verdict from the feature scenario.

    """
    assert summary.fields["bench"] == verdict, (
        f"expected changed-path verdict {verdict!r}; summary was {summary.fields!r}"
    )


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            SummaryCase("pull_request", Detector("success", "true"), "run"),
            id="relevant",
        ),
        pytest.param(
            SummaryCase("pull_request", Detector("success", "false"), "skip"),
            id="docs-only",
        ),
        pytest.param(
            SummaryCase("push", Detector("success", "false"), "run"),
            id="main-push",
        ),
        pytest.param(
            SummaryCase("workflow_dispatch", Detector("success", "false"), "run"),
            id="dispatch",
        ),
        pytest.param(
            SummaryCase(
                "pull_request", Detector("failure", ""), "skip-detector-failed"
            ),
            id="detector-failed",
        ),
        pytest.param(
            SummaryCase("pull_request", Detector("", ""), "skip-detector-failed"),
            id="detector-never-ran",
        ),
    ],
)
def test_the_recorded_decision_matches_the_gate(
    tmp_path: pth.Path, case: SummaryCase, workflow_data: Workflow
) -> None:
    """The recorded decision must match what the gate will actually do.

    The table is the whole input space the step has: two closed sets and an
    event name. Enumerating it keeps the summary from drifting away from the
    `if:` expression it describes, which is the failure a maintainer reading
    the summary could not detect.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Pytest temporary directory in which to capture the step summary.
    case : SummaryCase
        Event, detector, and expected decision supplied to the workflow script.

    """
    summary = run_summary_script(
        event=case.event,
        detector=case.detector,
        tmp_path=tmp_path,
        workflow_data=workflow_data,
    )

    assert summary.fields["decision"] == case.decision, (
        f"expected decision {case.decision!r}; summary was {summary.fields!r}"
    )
    assert summary.fields["event"] == case.event, (
        f"expected event {case.event!r}; summary was {summary.fields!r}"
    )
    expected_metric = _expected_metric(case)
    assert summary.metric == expected_metric, (
        f"expected bounded metric labels {expected_metric!r}; found {summary.metric!r}"
    )


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            SummaryCase("pull_request", Detector("success", "true"), "run"),
            id="run",
        ),
        pytest.param(
            SummaryCase("pull_request", Detector("success", "false"), "skip"),
            id="skip",
        ),
        pytest.param(
            SummaryCase(
                "pull_request", Detector("failure", ""), "skip-detector-failed"
            ),
            id="skip-detector-failed",
        ),
    ],
)
def test_the_summary_table_has_a_stable_canonical_form(
    tmp_path: pth.Path,
    case: SummaryCase,
    snapshot: SnapshotAssertion,
    workflow_data: Workflow,
) -> None:
    """The stable summary table renders the three benchmark outcomes."""
    summary = run_summary_script(
        event=case.event,
        detector=case.detector,
        tmp_path=tmp_path,
        workflow_data=workflow_data,
    )

    assert summary.table == snapshot, (
        "the canonical benchmark summary table must match its snapshot; "
        f"found:\n{summary.table}"
    )


def _expected_metric(case: SummaryCase) -> dict[str, str]:
    """Return the bounded metric expected for a summary case."""
    return {
        "event_class": "pull_request" if case.event == "pull_request" else "other",
        "detector_status": case.detector.outcome
        if case.detector.outcome in {"success", "failure"}
        else "unknown",
        "decision": case.decision,
    }
