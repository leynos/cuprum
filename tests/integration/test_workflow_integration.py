"""Run the real `changes` job under `act` and assert what it decided.

Each test here builds a throwaway repository whose history *is* the
changed-path set it wants to exercise, and runs the checked-in `changes` job
against it. Nothing is mocked: `dorny/paths-filter` really diffs, and the gate
step really reads its output.

The suite needs `act` and a container runtime, so it is opt-in
(`make test-act`) rather than part of `make test`. It skips with a stated
reason where either is missing, and `CUPRUM_REQUIRE_ACT=1` turns that skip into
a failure so a job that provides a runtime cannot report success for having run
nothing. See `docs/adr-012-actions-runner-integration-harness.md`.
"""

from __future__ import annotations

import pathlib as pth

import pytest

from tests.helpers.act_harness import (
    CHANGES_JOB,
    CI_WORKFLOW,
    ActRun,
    Event,
    branch,
    break_detector,
    commit_paths,
    harness_skip_reason,
    prepare_repository,
    run_act,
)

#: The repository under test, whose workflow and local actions are staged into
#: each scenario.
WORKTREE = pth.Path(__file__).resolve().parents[2]
#: One scenario invokes `act` on a fresh repository, and a cold image is the
#: only slow part; the suite-wide `timeout = 30` in pyproject.toml is far below
#: a warm run's 15-27s, so every scenario states its own bound.
SCENARIO_TIMEOUT = 300
#: The branch a pull-request scenario commits on. It must not be the default
#: branch: see `tests.helpers.act_harness.branch`.
FEATURE_BRANCH = "feature"
#: The detector's own output name, which the ratchet's `if:` consumes.
BENCH = "bench"
#: The three bounded values the gate publishes, and the telemetry sink labels.
GATE_OUTPUTS = ("event_class", "detector_status", "decision")

#: A path that the `bench` filter matches, and one it does not. Both are
#: ordinary files: the filter matches by path pattern, so no scenario needs a
#: real change to either subsystem to be relevant.
RELEVANT_PATH = "cuprum/thing.py"
IRRELEVANT_PATH = "docs/thing.md"
#: The detector's "a relevant path changed" answer, as the action reports it.
DETECTOR_TRUE = "true"
#: ...and the "nothing relevant changed" one.
DETECTOR_FALSE = "false"


@pytest.fixture(scope="session")
def act_available() -> str:
    """Decide once whether the harness can run, and pass that verdict down.

    Probing per test would repeat the filesystem checks and, worse, would let
    the skip reasons a report shows be decided in more than one place.
    ``CUPRUM_REQUIRE_ACT`` is read here too, so a refused skip fails the run
    rather than one test after another.

    Returns
    -------
    str
        The empty string, so the fixture can be depended on without a body.
    """
    reason = harness_skip_reason()
    if reason:
        pytest.skip(reason)
    return ""


@pytest.fixture
def scenario(tmp_path: pth.Path, act_available: str) -> pth.Path:
    """Return a prepared scenario repository, checked out on its base commit.

    Parameters
    ----------
    tmp_path : pathlib.Path
        pytest's per-test temporary directory.
    act_available : str
        The session probe, which skips the test when no runtime is present.

    Returns
    -------
    pathlib.Path
        A repository holding the checked-in workflow and one base commit.
    """
    return prepare_repository(tmp_path / "repo", WORKTREE)


def pull_request(repository: pth.Path, paths: list[str]) -> ActRun:
    """Commit ``paths`` on a feature branch and replay a pull request.

    Parameters
    ----------
    repository : pathlib.Path
        The prepared scenario repository.
    paths : list[str]
        The changed-path set the scenario is about.

    Returns
    -------
    ActRun
        What the job produced.
    """
    branch(repository, FEATURE_BRANCH)
    head = commit_paths(repository, paths, message="scenario change")
    return run_act(
        repository,
        Event(
            name="pull_request",
            payload={"action": "opened", "number": 1},
            ref="refs/pull/1/merge",
            sha=head,
            branch=FEATURE_BRANCH,
        ),
    )


def push(repository: pth.Path, paths: list[str]) -> ActRun:
    """Commit ``paths`` onto the default branch and replay a push to it.

    A push is the event class where the gate admits the ratchet regardless of
    the changed paths, so the scenario is about the decision, not the detector.

    Parameters
    ----------
    repository : pathlib.Path
        The prepared scenario repository.
    paths : list[str]
        The changed-path set the scenario is about.

    Returns
    -------
    ActRun
        What the job produced.
    """
    base = commit_paths(repository, [], message="base")
    head = commit_paths(repository, paths, message="scenario change")
    return run_act(
        repository,
        Event(
            name="push",
            payload={"before": base, "after": head, "ref": "refs/heads/main"},
            ref="refs/heads/main",
            sha=head,
            branch="main",
        ),
    )


def gate_row(run: ActRun) -> list[str]:
    """Return the summary table's single data row, as cells.

    The row is the recorded decision, so a test that asserts on it is asserting
    on what a maintainer auditing the run list would read.

    Parameters
    ----------
    run : ActRun
        The run whose summary to read.

    Returns
    -------
    list[str]
        The row's cells, stripped.
    """
    rows = [
        line
        for line in run.summary.splitlines()
        if line.startswith("|") and "---" not in line
    ]
    # The first row is the header; the scenario has exactly one decision.
    assert len(rows) == 2, f"expected one decision row, got:\n{run.summary}"
    return [cell.strip() for cell in rows[1].strip("|").split("|")]


def assert_decision(
    run: ActRun, bench: str | None, relevant: str, decision: str
) -> None:
    """Assert the detector's answer, the gate's outputs, and the table row.

    Three views of one decision are checked together because they are three
    ways a maintainer observes it: the `bench` output downstream jobs consume,
    the gate's published outputs, and the step summary.

    Parameters
    ----------
    run : ActRun
        The run to check.
    bench : str | None
        Expected `bench` output, or ``None`` when the detector failed and
        never set one.
    relevant : str
        The "performance-relevant changes" cell the table should show.
    decision : str
        Expected `decision` output, and the last table cell.
    """
    assert run.output(BENCH) == bench
    for name in GATE_OUTPUTS:
        assert run.output(name) is not None, f"{name} was never recorded"
    assert run.output("decision") == decision
    row = gate_row(run)
    assert row[2] == relevant
    assert row[-1] == decision


@pytest.mark.timeout(SCENARIO_TIMEOUT)
def test_a_relevant_pull_request_admits_the_benchmark_ratchet(
    scenario: pth.Path,
) -> None:
    """Relevant paths must set `bench` and admit the ratchet."""
    run = pull_request(scenario, [RELEVANT_PATH])
    assert run.exit_code == 0, run.failure_context()
    assert_decision(run, DETECTOR_TRUE, "true", "run")


@pytest.mark.timeout(SCENARIO_TIMEOUT)
def test_an_irrelevant_pull_request_skips_the_benchmark_ratchet(
    scenario: pth.Path,
) -> None:
    """A pull request touching nothing metered must not admit the ratchet."""
    run = pull_request(scenario, [IRRELEVANT_PATH])
    assert run.exit_code == 0, run.failure_context()
    assert_decision(run, DETECTOR_FALSE, "false", "skip")


@pytest.mark.timeout(SCENARIO_TIMEOUT)
def test_a_mixed_pull_request_is_relevant_if_any_path_is(
    scenario: pth.Path,
) -> None:
    """One relevant path among irrelevant ones must still admit the ratchet."""
    run = pull_request(scenario, [RELEVANT_PATH, IRRELEVANT_PATH])
    assert run.exit_code == 0, run.failure_context()
    assert_decision(run, DETECTOR_TRUE, "true", "run")


@pytest.mark.timeout(SCENARIO_TIMEOUT)
def test_an_empty_pull_request_skips_the_benchmark_ratchet(
    scenario: pth.Path,
) -> None:
    """An empty changed-path set is not a relevant one.

    The scenario commits nothing, so the detector must observe no changes at
    all — which is distinct from observing a change it does not match.
    """
    run = pull_request(scenario, [])
    assert run.exit_code == 0, run.failure_context()
    assert_decision(run, DETECTOR_FALSE, "false", "skip")


@pytest.mark.timeout(SCENARIO_TIMEOUT)
def test_a_push_runs_the_ratchet_even_without_relevant_paths(
    scenario: pth.Path,
) -> None:
    """A non-pull-request event admits the ratchet by event class alone.

    This is the case the `event_class` label exists for: the detector answered
    `false`, and the gate still decided `run`.
    """
    run = push(scenario, [IRRELEVANT_PATH])
    assert run.exit_code == 0, run.failure_context()
    assert_decision(run, DETECTOR_FALSE, "false", "run")
    assert run.output("event_class") == "other"
    assert gate_row(run)[0] == "push"


@pytest.mark.timeout(SCENARIO_TIMEOUT)
def test_a_failed_detector_still_records_a_decision(
    scenario: pth.Path,
) -> None:
    """Record `skip-detector-failed` rather than mistaking failure for `false`.

    The detector really fails here: the workflow is edited to pass the action
    an invalid value for one of its own inputs. `bench` is therefore never set,
    and the assertion is that the gate distinguishes "no answer" from "no
    relevant changes" — and that `benchmark-ratchet` is not admitted on a
    decision the detector could not make.
    """
    branch(scenario, FEATURE_BRANCH)
    commit_paths(scenario, [RELEVANT_PATH], message="scenario change")
    head = break_detector(scenario)
    run = run_act(
        scenario,
        Event(
            name="pull_request",
            payload={"action": "opened", "number": 1},
            ref="refs/pull/1/merge",
            sha=head,
            branch=FEATURE_BRANCH,
        ),
    )
    assert run.exit_code != 0, "the detector was expected to fail the job"
    assert "Detect performance-relevant changes" in run.failed_steps
    assert run.output(BENCH) is None, "a failed detector must not answer"
    assert_decision(run, None, "unknown", "skip-detector-failed")


def test_the_harness_verifies_the_job_the_gate_lives_in() -> None:
    """Guard the harness's own target against a renamed job or workflow.

    Everything above asserts on a job named `changes` in a particular file. If
    either were renamed, every scenario would fail with an `act` error that
    says nothing about the rename; this says it once, and cheaply.
    """
    source = (WORKTREE / CI_WORKFLOW).read_text(encoding="utf-8")
    assert f"\n  {CHANGES_JOB}:\n" in source
