"""Seed each cache-wiring fault and require its contract to reject it.

The cache contracts are only worth their length if they fail when the wiring
they describe is wrong. A contract parametrized over the correct estate passes
whether or not it discriminates, so each test here injects one fault into a
copy of the parsed steps, points the rule at the copy, and requires the real
rule to reject it.

The seeded faults are the ones the experiment's safety rests on: credentials
exported after sccache starts, an unpinned credentials action, a save with no
writer, a swapped fork arm, a backend predicate that differs from the runner's,
a compile step overriding the wrapper, a namespace taken from pull-request
input, and a ``pull_request_target`` trigger.
"""

from __future__ import annotations

import typing as typ

import pytest

import tests.helpers.ci_runners as runners
from tests.helpers.ci_cache_wiring import (
    privileged_cache_workflows,
    unsafe_namespaces,
    wrapper_overrides,
)
from tests.helpers.ci_compiler_evidence import EVIDENCE_ACTION_PATH
from tests.helpers.ci_expressions import ExpressionError
from tests.helpers.ci_workflows import (
    job,
    save_steps,
    steps,
    workflow_document,
    workflow_sources,
)
from tests.test_ci_cache_ownership import (
    test_saves_happen_only_on_trunk_and_only_after_a_miss,
)
from tests.test_ci_compiler_cache import (
    test_the_sccache_manifest_names_every_job_that_installs_the_wrapper,
)
from tests.test_ci_compiler_cache_arms import (
    SCENARIOS,
    test_the_backend_and_credentials_follow_the_runner_predicate,
)
from tests.test_ci_compiler_cache_backend import (
    CREDENTIALS_STEP,
    test_the_credentials_are_exported_before_sccache_is_set_up,
    test_the_credentials_step_runs_the_action_that_exports_them,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.workflow_types import Step

CREDENTIALS_WORKFLOW = "ci.yml"
CREDENTIALS_JOB = "coverage"


def _copy(job_steps: cabc.Sequence[Step]) -> list[Step]:
    """Return shallow copies, as the mutation seam expects."""
    return typ.cast("list[Step]", [dict(step) for step in job_steps])


def _named(mutated: cabc.Sequence[Step], name: str) -> Step:
    """Return the uniquely named step from a mutated list."""
    matches = [step for step in mutated if step.get("name") == name]
    assert len(matches) == 1, f"expected one {name!r}, found {len(matches)}"
    return matches[0]


def test_the_estate_satisfies_every_new_rule() -> None:
    """The positive control: today's workflows pass before a fault is seeded."""
    for workflow_name, _ in workflow_sources():
        declared = workflow_document(workflow_name).get("jobs", {})
        for job_name in typ.cast("dict[str, object]", declared):
            job_steps = steps(workflow_name, job_name)
            assert not wrapper_overrides(job_steps), (
                f"{workflow_name}:{job_name} overrides RUSTC_WRAPPER"
            )
            assert not unsafe_namespaces(job_steps), (
                f"{workflow_name}:{job_name} uses a non-literal namespace"
            )
    assert not privileged_cache_workflows([
        (name, workflow_document(name)) for name, _ in workflow_sources()
    ]), "no pull_request_target workflow may carry the experiment's steps"


def test_a_wrapper_override_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compile step that sets the wrapper itself must be refused."""
    real = steps("ci.yml", "extension-tests")

    def faulted(_workflow: str, _job: str) -> list[Step]:
        """Return the job with a compile step overriding the wrapper."""
        mutated = _copy(real)
        build = _named(mutated, "Build the native extension")
        typ.cast("dict[str, object]", build)["env"] = {
            "RUSTC_WRAPPER": "/nonexistent/not-sccache"
        }
        return mutated

    monkeypatch.setattr("tests.helpers.ci_cache_wiring.steps", faulted, raising=False)
    assert wrapper_overrides(faulted("ci.yml", "extension-tests")), (
        "the seeded override must be reported"
    )


def test_a_non_literal_namespace_is_detected() -> None:
    """A namespace taken from a pull-request value is not a fixed scope."""
    step = {
        "name": "Set up sccache",
        "uses": "./.github/actions/setup-sccache",
        "with": {"backend": "gha", "namespace": "${{ github.head_ref }}"},
    }
    assert unsafe_namespaces([step]), "a PR-controlled namespace must be refused"
    literal = {**step, "with": {"backend": "gha", "namespace": "py3.13-debug"}}
    assert not unsafe_namespaces([literal]), "a fixed namespace must be accepted"


def test_a_privileged_workflow_carrying_the_experiment_is_detected() -> None:
    """The privileged trigger must never carry a compiler-cache step."""
    document = {
        "on": {"pull_request_target": {}},
        "jobs": {"x": {"steps": [{"uses": EVIDENCE_ACTION_PATH}]}},
    }
    sources = [("ci.yml", typ.cast("dict[object, object]", document))]
    assert privileged_cache_workflows(sources) == ["ci.yml"]
    unprivileged = typ.cast(
        "dict[object, object]", {"on": {"pull_request": {}}, "jobs": document["jobs"]}
    )
    assert not privileged_cache_workflows([("ci.yml", unprivileged)]), (
        "an ordinary pull-request workflow may carry the experiment"
    )


def test_credentials_exported_after_startup_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swapping the two steps must fail the ordering rule, not merely read oddly."""
    real = steps(CREDENTIALS_WORKFLOW, CREDENTIALS_JOB)

    def faulted(_workflow: str, _job: str) -> list[Step]:
        """Return the job with the credentials step after sccache starts."""
        mutated = _copy(real)
        credentials = _named(mutated, CREDENTIALS_STEP)
        setup = _named(mutated, "Set up sccache")
        mutated.remove(credentials)
        mutated.insert(mutated.index(setup) + 1, credentials)
        return mutated

    monkeypatch.setattr("tests.test_ci_compiler_cache_backend.steps", faulted)
    with pytest.raises(AssertionError, match="must export the proxy credentials"):
        test_the_credentials_are_exported_before_sccache_is_set_up(
            CREDENTIALS_WORKFLOW, CREDENTIALS_JOB
        )


def test_an_unpinned_credentials_action_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tag can move; the reviewed exporter must stay pinned to a commit."""
    real = steps(CREDENTIALS_WORKFLOW, CREDENTIALS_JOB)

    def faulted(_workflow: str, _job: str) -> list[Step]:
        """Return the job with an unpinned credentials action."""
        mutated = _copy(real)
        credentials = _named(mutated, CREDENTIALS_STEP)
        credentials["uses"] = (
            "leynos/shared-actions/.github/actions/"
            "export-ubicloud-cache-credentials@main"
        )
        return mutated

    monkeypatch.setattr("tests.test_ci_compiler_cache_backend.steps", faulted)
    with pytest.raises(AssertionError, match="must pin a full 40-character"):
        test_the_credentials_step_runs_the_action_that_exports_them(
            CREDENTIALS_WORKFLOW, CREDENTIALS_JOB
        )


def test_a_save_without_trunk_guard_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pull request must never publish a generation."""
    real = save_steps("ci.yml", "extension-tests")

    def faulted(_workflow: str, _job: str) -> list[Step]:
        """Return the save steps with the trunk guard removed."""
        mutated = _copy(real)
        _named(mutated, "Save the compiler cache")["if"] = "always()"
        return mutated

    monkeypatch.setattr("tests.test_ci_cache_ownership.save_steps", faulted)
    with pytest.raises(AssertionError, match="guarded by"):
        test_saves_happen_only_on_trunk_and_only_after_a_miss()


def test_a_swapped_fork_arm_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arms are read by position, so a swap must fail the evaluated test."""
    real_runs_on = job("ci.yml", "coverage").get("runs-on")
    swapped = (
        "${{ (github.event.pull_request.head.repo.fork || "
        "github.event.pull_request.user.login == 'dependabot[bot]') "
        "&& 'ubicloud-standard-2' || 'ubuntu-latest' }}"
    )

    def faulted_job(workflow_name: str, job_name: str) -> dict[str, object]:
        """Return the job with its runner arms swapped."""
        declared = dict(job(workflow_name, job_name))
        if job_name == "coverage":
            declared["runs-on"] = swapped
        return declared

    monkeypatch.setattr("tests.test_ci_compiler_cache_arms.job", faulted_job)
    with pytest.raises((AssertionError, ExpressionError)):
        test_the_backend_and_credentials_follow_the_runner_predicate(
            "ci.yml", "coverage", SCENARIOS[0]
        )
    assert real_runs_on, "the real runner declaration must exist"


def test_a_divergent_backend_predicate_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that drops the Dependabot arm would send it to Ubicloud."""
    real = steps("ci.yml", "coverage")

    def faulted(_workflow: str, _job: str) -> list[Step]:
        """Return the job with a backend predicate that drops Dependabot."""
        mutated = _copy(real)
        setup = _named(mutated, "Set up sccache")
        declared = dict(typ.cast("dict[str, object]", setup["with"]))
        declared["backend"] = (
            "${{ github.event.pull_request.head.repo.fork && 'local' || 'gha' }}"
        )
        setup["with"] = declared
        return mutated

    monkeypatch.setattr("tests.test_ci_compiler_cache_arms.steps", faulted)
    with pytest.raises(AssertionError, match="different predicate"):
        test_the_backend_and_credentials_follow_the_runner_predicate(
            "ci.yml", "coverage", SCENARIOS[0]
        )


def test_dropping_a_job_from_the_sccache_manifest_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job that installs the wrapper must stay inside the contract."""
    monkeypatch.setattr(
        "tests.test_ci_compiler_cache.SCCACHE_JOBS",
        tuple(
            pair
            for pair in runners.SCCACHE_JOBS
            if pair != ("ci.yml", "typecheck-test")
        ),
    )
    with pytest.raises(AssertionError, match="SCCACHE_JOBS must name exactly"):
        test_the_sccache_manifest_names_every_job_that_installs_the_wrapper()
