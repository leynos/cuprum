"""Contract tests for cache ownership on Cuprum's Ubicloud jobs.

A cache is only an optimization when exactly one job writes each key, every
restore precedes the work it is meant to avoid, and a miss is visible in the
run summary. None of that is observable from a green run: a job that
re-downloads its whole dependency graph on every push looks identical to one
that restored it. These tests read the ownership back from the workflows.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.ci_cache_families import CacheFamily, writer_families
from tests.helpers.ci_runners import (
    CACHE_FAMILY_WRITERS,
    CACHE_KEYS_ACTION,
    CACHE_RESTORE,
    CACHE_WRITERS,
    CACHED_JOBS,
    GENERATE_COVERAGE,
    KEY_SHARING_WORKFLOWS,
    MAKE_DRIVEN_JOBS,
    MAKE_UV_PATHS,
    OBSERVATION_STEP,
    ROLLING_KEYS,
    SHARED_KEY_INPUTS,
    cache_paths,
    cache_steps,
    expand,
    restore_steps,
    save_steps,
    step_inputs,
    steps,
    workflow_env,
)

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

CACHED_CASES = expand(CACHED_JOBS)
MAKE_DRIVEN_CASES = expand(MAKE_DRIVEN_JOBS)
#: Steps that consume a cache. A restore declared after any of them has already
#: missed the work it was meant to avoid.
WORK_STEP_PREFIXES = ("install", "build", "run", "generate", "prepare", "set up")
#: Every clause a save step must carry, so the trusted generation is writable
#: only from trunk.
SAVE_GUARD_CLAUSES = (
    "github.event_name == 'push'",
    "github.ref == 'refs/heads/main'",
)
#: The additional clause a content-addressed key carries, so a run that already
#: hit does not re-upload what it just downloaded.
MISS_GUARD_CLAUSE = "outputs.cache-hit != 'true'"


def _key_of(step: Step, message: str) -> str:
    """Return a cache step's ``key`` input, asserting it is a string."""
    key = step_inputs(step, message).get("key")
    assert isinstance(key, str), f"{message}: key must be a string"
    return key


def _key_name(key_expression: str) -> str:
    """Return the ``env`` name a cache key expression renders from."""
    inner = key_expression.strip().removeprefix("${{").removesuffix("}}").strip()
    assert inner.startswith("env."), (
        f"cache keys must render from a named env value, got {key_expression!r}"
    )
    return inner.removeprefix("env.")


@pytest.mark.parametrize(("workflow_name", "job_name"), CACHED_CASES)
def test_each_cached_path_has_exactly_one_owner(
    workflow_name: str, job_name: str
) -> None:
    """Give every mutable path one key, so two steps cannot fight over it."""
    owners: dict[str, str] = {}
    for step in cache_steps(workflow_name, job_name):
        message = f"{workflow_name}:{job_name} cache step must declare paths"
        key = _key_of(step, message)
        for path in cache_paths(step, message):
            existing = owners.setdefault(path, key)
            assert existing == key, (
                f"{workflow_name}:{job_name} gives {path} two owners: "
                f"{existing} and {key}"
            )


@pytest.mark.parametrize(("workflow_name", "job_name"), CACHED_CASES)
def test_restores_precede_the_work_they_avoid(
    workflow_name: str, job_name: str
) -> None:
    """Mount every cache before the first install, build, or test command."""
    job_steps = steps(workflow_name, job_name)
    last_restore = max(
        index
        for index, step in enumerate(job_steps)
        if step.get("uses") == CACHE_RESTORE
    )
    work_indices = [
        index
        for index, step in enumerate(job_steps)
        if str(step.get("name", "")).lower().startswith(WORK_STEP_PREFIXES)
    ]
    assert work_indices, f"{workflow_name}:{job_name} must do some work"
    assert last_restore < min(work_indices), (
        f"{workflow_name}:{job_name} must restore every cache before "
        f"{job_steps[min(work_indices)].get('name')!r}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), CACHED_CASES)
def test_cache_keys_come_from_the_shared_renderer(
    workflow_name: str, job_name: str
) -> None:
    """Render each key once, so a restore and its save cannot disagree."""
    job_steps = steps(workflow_name, job_name)
    renderers = [step for step in job_steps if step.get("uses") == CACHE_KEYS_ACTION]
    assert len(renderers) == 1, (
        f"{workflow_name}:{job_name} must render its keys through "
        f"{CACHE_KEYS_ACTION} exactly once"
    )
    first_cache = min(
        index
        for index, step in enumerate(job_steps)
        if step.get("uses") == CACHE_RESTORE
    )
    assert job_steps.index(renderers[0]) < first_cache, (
        f"{workflow_name}:{job_name} must render its keys before restoring"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), CACHED_CASES)
def test_every_restore_falls_back_to_its_generation_prefix(
    workflow_name: str, job_name: str
) -> None:
    """Let a warm run start from the latest generation rather than from nothing."""
    for step in restore_steps(workflow_name, job_name):
        message = f"{workflow_name}:{job_name} restore must declare inputs"
        inputs = step_inputs(step, message)
        key_name = _key_name(_key_of(step, message))
        expected = f"${{{{ env.{key_name.removesuffix('KEY')}PREFIX }}}}"
        assert inputs.get("restore-keys") == expected, (
            f"{workflow_name}:{job_name} restore of {key_name} must fall back to "
            f"{expected}"
        )


def _observed_writers() -> dict[str, list[tuple[str, str]]]:
    """Return every save step in the estate, grouped by the key it publishes."""
    observed: dict[str, list[tuple[str, str]]] = {}
    for workflow_name, job_name in expand(CACHED_JOBS):
        for step in save_steps(workflow_name, job_name):
            message = f"{workflow_name}:{job_name} save must declare inputs"
            observed.setdefault(_key_name(_key_of(step, message)), []).append((
                workflow_name,
                job_name,
            ))
    return observed


def test_each_key_has_exactly_one_writer_per_lane() -> None:
    """Stop two jobs racing to publish one cold key on the same push."""
    observed = _observed_writers()
    assert {key: tuple(sorted(writers)) for key, writers in observed.items()} == {
        key: tuple(sorted(writers)) for key, writers in CACHE_WRITERS.items()
    }, f"cache writers drifted from the manifest: {observed}"


def test_each_cache_family_has_exactly_one_writer() -> None:
    """Give every rendered archive one owner, not every ``env`` name one owner.

    The name a save step writes is not the archive it publishes. Five jobs name
    ``SCCACHE_CACHE_KEY`` and write five disjoint families, because the
    rendered key carries the runner lane, the interpreter and the build shape.
    A contract that counted names would have to permit all five collisions, and
    would then also permit a real one.
    """
    observed: dict[CacheFamily, tuple[str, str]] = {}
    for workflow_name, job_name in expand(CACHED_JOBS):
        for family in writer_families(workflow_name, job_name):
            first = observed.setdefault(family, (workflow_name, job_name))
            assert first == (workflow_name, job_name), (
                f"{family} is written by both {first} and {(workflow_name, job_name)}"
            )
    expected = {
        CacheFamily(*declared): writer
        for declared, writer in CACHE_FAMILY_WRITERS.items()
    }
    assert observed == expected, (
        "cache families drifted from the manifest; observed "
        f"{sorted(map(str, observed))}"
    )


def test_saves_happen_only_on_trunk_and_only_after_a_miss() -> None:
    """Keep pull requests reading the trusted generation without writing to it."""
    for workflow_name, job_name in expand(CACHED_JOBS):
        for step in save_steps(workflow_name, job_name):
            condition = step.get("if")
            assert isinstance(condition, str), (
                f"{workflow_name}:{job_name} save must be conditional"
            )
            collapsed = " ".join(condition.split())
            message = f"{workflow_name}:{job_name} save must declare inputs"
            required = list(SAVE_GUARD_CLAUSES)
            if _key_name(_key_of(step, message)) not in ROLLING_KEYS:
                required.append(MISS_GUARD_CLAUSE)
            missing = [clause for clause in required if clause not in collapsed]
            assert not missing, (
                f"{workflow_name}:{job_name} save must be guarded by {missing}; "
                f"got {collapsed!r}"
            )


@pytest.mark.parametrize(("workflow_name", "job_name"), CACHED_CASES)
def test_every_restore_is_reported_in_the_run_summary(
    workflow_name: str, job_name: str
) -> None:
    """Make a cold cache readable in the summary rather than inferred from timing."""
    reports = [
        step
        for step in steps(workflow_name, job_name)
        if step.get("name") == OBSERVATION_STEP
    ]
    assert len(reports) == 1, (
        f"{workflow_name}:{job_name} must record its cache observations once"
    )
    report = reports[0]
    assert report.get("if") == "always()", (
        f"{workflow_name}:{job_name} must record observations even on failure"
    )
    script = report.get("run")
    assert isinstance(script, str), (
        f"{workflow_name}:{job_name} observations must run a script"
    )
    assert "GITHUB_STEP_SUMMARY" in script, (
        f"{workflow_name}:{job_name} must write the observations to the summary"
    )
    environment = report.get("env")
    assert isinstance(environment, dict), (
        f"{workflow_name}:{job_name} must pass each restore result through env"
    )
    for step in restore_steps(workflow_name, job_name):
        step_id = step.get("id")
        assert isinstance(step_id, str), (
            f"{workflow_name}:{job_name} every restore must carry an id"
        )
        assert any(
            f"steps.{step_id}.outputs.cache-hit" in str(value)
            for value in environment.values()
        ), f"{workflow_name}:{job_name} must report the {step_id!r} result"
        key_name = _key_name(
            _key_of(step, f"{workflow_name}:{job_name} restore must declare inputs")
        )
        assert f"${{{key_name}}}" in script, (
            f"{workflow_name}:{job_name} must print the rendered {key_name}"
        )


@pytest.mark.parametrize(("workflow_name", "job_name"), MAKE_DRIVEN_CASES)
def test_make_driven_jobs_cache_the_worktree_uv_directories(
    workflow_name: str, job_name: str
) -> None:
    """Cache the uv directories Make pins rather than uv's default locations."""
    mounted = {
        path
        for step in restore_steps(workflow_name, job_name)
        for path in cache_paths(step, f"{workflow_name}:{job_name} restore")
    }
    for expected in MAKE_UV_PATHS:
        assert expected in mounted, (
            f"{workflow_name}:{job_name} installs through Make, so it must cache "
            f"{expected}; got {sorted(mounted)}"
        )


@pytest.mark.parametrize(("workflow_name", "job_name"), MAKE_DRIVEN_CASES)
def test_tool_caching_uses_the_installed_tool_parent(
    workflow_name: str, job_name: str
) -> None:
    """Cache the directory, never the executable: a cold mount shadows the file."""
    mounted = {
        path
        for step in restore_steps(workflow_name, job_name)
        for path in cache_paths(step, f"{workflow_name}:{job_name} restore")
    }
    assert "~/.local/bin" in mounted, (
        f"{workflow_name}:{job_name} must cache the installed-tool parent"
    )
    assert "~/.local/bin/sccache" not in mounted, (
        f"{workflow_name}:{job_name} must not cache a binary as a directory"
    )


@pytest.mark.parametrize(
    ("workflow_name", "job_name"),
    [("ci.yml", "coverage"), ("coverage-main.yml", "coverage-upload")],
)
def test_coverage_delegates_archive_cache_ownership(
    workflow_name: str, job_name: str
) -> None:
    """Ensure the coverage action leaves dependency archives to the caller."""
    coverage_step = next(
        step
        for step in steps(workflow_name, job_name)
        if step.get("uses") == GENERATE_COVERAGE
    )
    inputs = step_inputs(
        coverage_step, f"{workflow_name}:{job_name} coverage must declare inputs"
    )
    assert inputs.get("cache-provider") == "external", (
        f"{workflow_name}:{job_name} must delegate cache ownership externally"
    )


def test_workflows_that_share_a_key_share_its_inputs() -> None:
    """Keep a restore in one workflow able to hit an archive another wrote."""
    values = {
        workflow_name: {
            name: workflow_env(workflow_name)[name] for name in SHARED_KEY_INPUTS
        }
        for workflow_name in KEY_SHARING_WORKFLOWS
    }
    reference, *others = values.values()
    for name, observed in zip(KEY_SHARING_WORKFLOWS[1:], others, strict=True):
        assert observed == reference, (
            f"{name} renders the tool cache key from {observed}, but "
            f"{KEY_SHARING_WORKFLOWS[0]} uses {reference}"
        )


def test_coverage_checkouts_do_not_persist_credentials() -> None:
    """Keep the scoped token out of .git/config where project code could read it."""
    for workflow_name, job_name in (
        ("ci.yml", "coverage"),
        ("coverage-main.yml", "coverage-upload"),
    ):
        checkout = next(
            step
            for step in steps(workflow_name, job_name)
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        inputs = step_inputs(
            checkout, f"{workflow_name}:{job_name} checkout must declare inputs"
        )
        assert inputs.get("persist-credentials") is False, (
            f"{workflow_name}:{job_name} must set persist-credentials: false"
        )
