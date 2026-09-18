"""Contract tests for where Cuprum's CI jobs run and whether they run.

Runner placement is a declaration no functional test can reach: a job that
drifts back to a GitHub-hosted label, asks for a paid runner a fork can never
obtain, or carries no ceiling at all still produces a green suite while
queueing for hours or billing for six. These tests read the declarations back
against the manifests in `tests/helpers/ci_runners.py`, through the one reader
in `tests/helpers/ci_placement.py`.

Companion modules cover the neighbouring declarations: the label registry in
`test_ci_actionlint_registry.py`, worker counts in `test_ci_worker_bounds.py`,
and tool installation in `test_ci_tool_installation.py`.
"""

from __future__ import annotations

import pytest
import yaml

from tests.helpers.ci_runners import (
    CONTINUE_ON_ERROR_JOBS,
    EXPERIMENTAL_LEG_KEY,
    FORK_FIELD,
    FORK_REACHABLE_UBICLOUD_JOBS,
    GITHUB_HOSTED_JOBS,
    GITHUB_LABEL,
    UBICLOUD_JOBS,
    UBICLOUD_LABEL,
    WINDOWS_HOSTED_JOBS,
    WINDOWS_LABEL,
    all_jobs,
    declares_steps,
    expand,
    job,
    never_runs,
    placement,
    references,
    workflow_document,
    workflow_sources,
)

UBICLOUD_CASES = expand(UBICLOUD_JOBS)
GITHUB_HOSTED_CASES = expand(GITHUB_HOSTED_JOBS)
WINDOWS_HOSTED_CASES = expand(WINDOWS_HOSTED_JOBS)
FORK_REACHABLE_CASES = expand(FORK_REACHABLE_UBICLOUD_JOBS)
ALL_CASES = all_jobs()
STEP_CASES = [case for case in ALL_CASES if declares_steps(*case)]
CALLER_CASES = [case for case in ALL_CASES if not declares_steps(*case)]


@pytest.mark.parametrize(("workflow_name", "job_name"), UBICLOUD_CASES)
def test_linux_build_and_test_jobs_use_the_ubicloud_default_shape(
    workflow_name: str, job_name: str
) -> None:
    """Keep every repository-owned Linux gate on the reviewed Ubicloud shape."""
    owned = placement(workflow_name, job_name).owned
    assert owned == UBICLOUD_LABEL, (
        f"{workflow_name}:{job_name} must run on {UBICLOUD_LABEL}; escalating to a "
        f"larger shape needs recorded measurements, got {owned!r}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), FORK_REACHABLE_CASES)
def test_fork_reachable_lanes_fall_back_by_position(
    workflow_name: str, job_name: str
) -> None:
    """Send a fork's pull request to a runner it can actually obtain.

    Read by position, not by membership. Nile-valley #106's contract asserted
    that the expression named one hosted and one Ubicloud label somewhere, so
    swapping the arms passed while sending forks to a runner no fork can get.
    The condition is full-matched too: a sibling field such as
    `head.repo.private` produces a well-formed expression that branches on the
    wrong thing.
    """
    placed = placement(workflow_name, job_name)
    assert placed.kind == "fork", (
        f"{workflow_name}:{job_name} must select its runner with the fork "
        f"fallback expression, got a {placed.kind} placement"
    )
    assert placed.references == frozenset({FORK_FIELD}), (
        f"{workflow_name}:{job_name} must branch on {FORK_FIELD}, "
        f"got {sorted(placed.references)}"
    )
    assert placed.fork == GITHUB_LABEL, (
        f"{workflow_name}:{job_name} must send a fork to {GITHUB_LABEL}, "
        f"got {placed.fork!r}"
    )
    assert placed.owned == UBICLOUD_LABEL, (
        f"{workflow_name}:{job_name} must keep {UBICLOUD_LABEL} on the owned "
        f"arm, got {placed.owned!r}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), STEP_CASES)
def test_placement_expressions_parse_to_one_line(
    workflow_name: str, job_name: str
) -> None:
    """Refuse a folded scalar whose continuation kept its line break.

    A continuation indented one level deeper puts a newline inside the
    expression. GitHub evaluates the broken value regardless, so a green run is
    no evidence; only the parsed document shows it (dev-env-rocky #216).

    Reading the declaration is the assertion: the reader refuses an embedded
    line break, a list, an absent value and any expression it cannot model.
    """
    placement(workflow_name, job_name)


def test_fork_reachability_matches_the_manifest() -> None:
    """Hold the fallback manifest against the triggers it rests on.

    `runs-on` can never check the premise these rules depend on: that a fork
    can reach the lane at all. Without the assertion a silent trigger change
    leaves the consequence looking deliberate (falcon-pachinko). YAML 1.1 reads
    the bare word `on` as the boolean `True`, so the triggers are read under
    that key; reading the string `"on"` would find nothing and pass vacuously.
    """
    ci_triggers = workflow_document("ci.yml")[True]
    assert isinstance(ci_triggers, dict), "ci.yml must declare its triggers"
    assert "pull_request" in ci_triggers, (
        "ci.yml must declare the pull_request trigger; without it no fork "
        "reaches these lanes and every fallback arm below is dead code"
    )
    caller = job("ci.yml", "build-wheels").get("uses")
    assert caller == "./.github/workflows/build-wheels.yml", (
        "ci.yml must call build-wheels.yml; that call, not build-wheels.yml's "
        "own workflow_call trigger, is what exposes its jobs to forks"
    )
    coverage_triggers = workflow_document("coverage-main.yml")[True]
    assert isinstance(coverage_triggers, dict), (
        "coverage-main.yml must declare its triggers"
    )
    assert "pull_request" not in coverage_triggers, (
        "coverage-main.yml is absent from the fork manifest because no fork "
        "can trigger it; a pull_request trigger here would make that false"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), STEP_CASES)
def test_every_job_running_steps_declares_a_ceiling(
    workflow_name: str, job_name: str
) -> None:
    """Bound a wedged runner rather than paying for the six-hour default.

    Keyed on declaring steps rather than on carrying an Ubicloud label. Once a
    label is an expression, "an Ubicloud lane" is a property of the event, so a
    rule keyed on the label stops applying on exactly the arm that hangs.
    """
    timeout = job(workflow_name, job_name).get("timeout-minutes")
    assert isinstance(timeout, int), (
        f"{workflow_name}:{job_name} must declare timeout-minutes, got {timeout!r}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), CALLER_CASES)
def test_reusable_workflow_callers_declare_no_ceiling(
    workflow_name: str, job_name: str
) -> None:
    """Keep placing a job and bounding it as separate ideas.

    GitHub rejects `timeout-minutes` on a job with `uses:`, so the bound lives
    in the callee. Each of this repository's callers calls a workflow whose own
    jobs are bounded by the rule above (weaver).
    """
    declared = job(workflow_name, job_name)
    assert "uses" in declared, (
        f"{workflow_name}:{job_name} declares neither steps nor uses"
    )
    assert declared.get("timeout-minutes") is None, (
        f"{workflow_name}:{job_name} calls a reusable workflow, where GitHub "
        "rejects timeout-minutes; bound the callee instead"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), ALL_CASES)
def test_no_reviewed_lane_is_switched_off(workflow_name: str, job_name: str) -> None:
    """Read whether a job runs, not only how it is configured.

    Every placement, budget and cache rule in this repository reads a
    declaration. `if: false` leaves all of them satisfied and runs nothing
    (lille #349). A containment test would accept `false && matrix.x == 'y'`,
    so the leading clause is matched explicitly.
    """
    assert not never_runs(workflow_name, job_name), (
        f"{workflow_name}:{job_name} can never run, so every rule about where "
        "it runs and what it costs holds vacuously"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), ALL_CASES)
def test_only_the_experimental_matrix_leg_may_fail_silently(
    workflow_name: str, job_name: str
) -> None:
    """Keep `continue-on-error` to the leg that is allowed to be broken."""
    declared = job(workflow_name, job_name).get("continue-on-error")
    if declared is None:
        return
    assert (workflow_name, job_name) in CONTINUE_ON_ERROR_JOBS, (
        f"{workflow_name}:{job_name} tolerates its own failure but is not in "
        "the manifest; a gate that cannot fail the workflow gates nothing"
    )
    assert references(declared) == frozenset({f"matrix.{EXPERIMENTAL_LEG_KEY}"}), (
        f"{workflow_name}:{job_name} must tolerate failure only on the leg its "
        f"matrix marks {EXPERIMENTAL_LEG_KEY!r}, got {declared!r}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), STEP_CASES)
def test_job_names_do_not_follow_their_runner(
    workflow_name: str, job_name: str
) -> None:
    """Keep a required check's context stable across events.

    With the fork fallback the runner follows the event, so a name that reads
    the runner renders differently on a fork's pull request and an internal
    one, and no single required context exists on both. Resolved through the
    matrix: rstest-bdd #788's contract compared the name's references with
    `runs-on`'s, which read `matrix.os` and stopped, while the matrix value
    behind that key held the fork expression.
    """
    declared_name = job(workflow_name, job_name).get("name")
    shared = references(declared_name) & placement(workflow_name, job_name).references
    assert not shared, (
        f"{workflow_name}:{job_name} names itself from {sorted(shared)}, which "
        "its runner also reads; the check context would follow the event"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), GITHUB_HOSTED_CASES)
def test_administrative_and_serial_jobs_stay_github_hosted(
    workflow_name: str, job_name: str
) -> None:
    """Keep sleeping, API-bound, and publish-only work off metered build slots."""
    placed = placement(workflow_name, job_name)
    assert placed.labels == frozenset({GITHUB_LABEL}), (
        f"{workflow_name}:{job_name} must stay on {GITHUB_LABEL}, "
        f"got {sorted(placed.labels)}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), WINDOWS_HOSTED_CASES)
def test_windows_native_jobs_stay_on_github_hosted_windows(
    workflow_name: str, job_name: str
) -> None:
    """Keep native Windows validation on the reviewed hosted runner image.

    A hosted-at-all predicate is not a placement predicate: an API-bound job
    moved to windows-latest satisfied a contract whose message named
    ubuntu-latest (lille #349), so the label is asserted by equality.
    """
    placed = placement(workflow_name, job_name)
    assert placed.labels == frozenset({WINDOWS_LABEL}), (
        f"{workflow_name}:{job_name} must stay on {WINDOWS_LABEL}, "
        f"got {sorted(placed.labels)}"
    )


def test_native_wheel_matrix_keeps_its_platform_runners() -> None:
    """Ubicloud has no Windows or macOS capacity, so the matrix stays hosted."""
    matrix_job = job("build-wheels.yml", "build-native-wheels")
    placed = placement("build-wheels.yml", "build-native-wheels")
    assert placed.kind == "matrix", (
        "build-wheels.yml:build-native-wheels must keep its platform matrix"
    )
    assert UBICLOUD_LABEL not in placed.labels, (
        "both ubuntu legs are named verbatim in the main-required-checks "
        "ruleset, so moving either renames a required context; that is the "
        "repository owner's decision, not a placement change"
    )
    strategy = matrix_job.get("strategy")
    assert isinstance(strategy, dict), "the native wheel job must declare a strategy"
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), "the native wheel strategy must declare a matrix"
    include = matrix.get("include")
    assert isinstance(include, list), "the native wheel matrix must list its legs"
    operating_systems = {entry["os"] for entry in include}
    assert {"windows-2022", "macos-latest", "macos-15-intel"} <= operating_systems, (
        "the native matrix must retain its Windows and macOS legs"
    )


def test_every_workflow_job_appears_in_one_placement_manifest() -> None:
    """Fail on a new job rather than letting it choose a runner unreviewed."""
    declared = {
        (workflow_name, job_name)
        for workflow_name, source in workflow_sources()
        for job_name in (yaml.safe_load(source).get("jobs") or {})
    }
    known = (
        set(UBICLOUD_CASES)
        | set(GITHUB_HOSTED_CASES)
        | set(WINDOWS_HOSTED_CASES)
        | {
            ("build-wheels.yml", "build-native-wheels"),
            # Callers of a reusable workflow declare no runner of their own.
            ("ci.yml", "build-wheels"),
            ("release.yml", "build-wheels"),
            ("dependabot-automerge.yml", "automerge"),
            ("mutation-testing.yml", "mutation-python"),
        }
    )
    assert declared == known, (
        "every workflow job must be classified in tests/helpers/ci_runners.py; "
        f"unclassified: {sorted(declared - known)}; stale: {sorted(known - declared)}"
    )


def test_ci_can_be_dispatched_for_warm_cache_measurement() -> None:
    """Keep a way to re-run CI on trunk without inventing a code change.

    Warm-cache evidence needs the same workflow run twice over an unchanged
    tree. A dispatch restores every cache and saves none, because every save
    guard requires a `push` event, so repeated dispatches read the trusted
    generation rather than churning it.
    """
    document = workflow_document("ci.yml")
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict), "ci.yml must declare its triggers"
    assert "workflow_dispatch" in triggers, (
        "ci.yml must stay dispatchable: without it the only way to produce a "
        "warm run on `main` is to push a change, which is not the same tree"
    )
