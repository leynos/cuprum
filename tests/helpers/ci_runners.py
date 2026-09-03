"""Shared manifests and loaders for the CI runner and cache contract tests.

The repository's Linux build and test jobs run on Ubicloud managed runners and
own every cache they depend on. Neither property is exercised by ordinary
tests: a workflow that silently moves back to a GitHub-hosted label, or grows a
second owner for `~/.cargo/registry`, still passes every functional suite. The
manifests below are therefore the declared intent, and the tests in
``tests/test_ci_runner_placement.py`` and ``tests/test_ci_cache_ownership.py``
read the workflows back against them.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

import yaml

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.workflow_types import Job, Step

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"

#: The one Ubicloud shape this repository uses. The recipe treats
#: `ubicloud-standard-4` as a ceiling reached only with measured evidence, and
#: no job here has produced that evidence.
UBICLOUD_LABEL = "ubicloud-standard-2"
#: vCPUs behind ``UBICLOUD_LABEL``. Test and build concurrency is bounded by
#: this number because the job is billed for exactly these cores.
UBICLOUD_VCPUS = 2
GITHUB_LABEL = "ubuntu-latest"

#: Ubicloud's transparent cache intercepts `actions/cache` at this version, so
#: a Linux archive written on an Ubicloud runner lands in Ubicloud's store
#: rather than GitHub's. Verified against the Ubicloud console listings on
#: 2026-09-03; v4.3.0 left nothing there. The deprecated `ubicloud/cache` fork
#: is therefore unnecessary.
CACHE_ACTION_PIN = "55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
CACHE_RESTORE = f"actions/cache/restore@{CACHE_ACTION_PIN}"
CACHE_SAVE = f"actions/cache/save@{CACHE_ACTION_PIN}"
CACHE_PLAIN = f"actions/cache@{CACHE_ACTION_PIN}"
CACHE_KEYS_ACTION = "./.github/actions/cache-keys"
SCCACHE_ACTION = "./.github/actions/setup-sccache"
NEXTEST_INSTALLER = "taiki-e/install-action@18b1216eba7f8039b0f8d131d5473787f0edce68"
SETUP_RUST = (
    "leynos/shared-actions/.github/actions/setup-rust@"
    "5daae0a332441d170d88ca648c9e71f0bbe96cb3"
)
#: Interim pin. This revision drops `target` from the action's own cache, which
#: is what lets the caller hold it to `cache-provider: external` without the
#: shared action re-adding a compiler-output archive. The nextest
#: no-source-fallback change is still open upstream, so a further repin follows.
GENERATE_COVERAGE = (
    "leynos/shared-actions/.github/actions/generate-coverage@"
    "7c9d66030879b504365202df90f439ea419e72bd"
)
OBSERVATION_STEP = "Record cache observations"

#: Repository-owned Linux build and test jobs. Every one is developer-blocking
#: and does real work, which is what buys it a paid runner.
UBICLOUD_JOBS: typ.Final[cabc.Mapping[str, tuple[str, ...]]] = {
    "build-wheels.yml": ("build-pure-wheel", "verify-wheel-install"),
    "ci.yml": ("typecheck-test", "extension-tests", "coverage", "benchmark-ratchet"),
    "coverage-main.yml": ("coverage-upload",),
}
#: Jobs that stay on GitHub-hosted runners, and why. Ubicloud offers Linux
#: only, and a job that sleeps, calls an API, or publishes an artefact someone
#: else built gains nothing from a metered build slot.
GITHUB_HOSTED_JOBS: typ.Final[cabc.Mapping[str, tuple[str, ...]]] = {
    "ci.yml": ("lint-test", "changes"),
    "delayed-pr-comment.yml": ("delay_and_comment",),
    "get-codescene-sha.yml": ("refresh-sha",),
    "release.yml": ("publish",),
}
#: Jobs that restore at least one cache through the shared renderer.
CACHED_JOBS: typ.Final[cabc.Mapping[str, tuple[str, ...]]] = {
    "ci.yml": (
        "lint-test",
        "typecheck-test",
        "extension-tests",
        "coverage",
        "benchmark-ratchet",
    ),
    "coverage-main.yml": ("coverage-upload",),
}
#: Jobs whose dependency installation runs through Make. `Makefile` pins
#: `UV_CACHE_DIR=.uv-cache` and `UV_TOOL_DIR=.uv-tools`, so uv's standard
#: directories stay empty and the worktree-local pair must be cached too.
MAKE_DRIVEN_JOBS: typ.Final[cabc.Mapping[str, tuple[str, ...]]] = {
    "ci.yml": ("typecheck-test", "extension-tests", "coverage", "benchmark-ratchet"),
    "coverage-main.yml": ("coverage-upload",),
}
MAKE_UV_PATHS: typ.Final = (".uv-cache", ".uv-tools")
#: Every job that compiles Rust. sccache is the single owner of compiler
#: output for every build shape in this repository, so each of these installs
#: the wrapper and reports its counters, and none of them archives `target`.
SCCACHE_JOBS: typ.Final = (
    ("ci.yml", "lint-test"),
    ("ci.yml", "typecheck-test"),
    ("ci.yml", "extension-tests"),
    ("ci.yml", "coverage"),
    ("ci.yml", "benchmark-ratchet"),
    ("coverage-main.yml", "coverage-upload"),
)
#: Paths no cache step may ever carry. A `target` tree is invalidated far more
#: often than the registry beside it, and sccache already holds the objects it
#: would preserve, keyed by the flags that distinguish the debug, cranelift,
#: and coverage-instrumented shapes.
FORBIDDEN_CACHE_PATHS: typ.Final = ("target", "rust/target", "target/debug")

#: One writer per key. Every other job restores. Pull requests never save: a
#: pull-request branch cannot publish the trusted generation, and the attempt
#: only produces `Unable to reserve cache` noise and wasted upload time.
#: One writer per key *per lane*. Every key carries `runner.environment`, so
#: the GitHub-hosted lane and the Ubicloud lane render different values and
#: read different cache services; a key with two writers has one on each side.
CACHE_WRITERS: typ.Final[cabc.Mapping[str, tuple[tuple[str, str], ...]]] = {
    "CARGO_CACHE_KEY": (("ci.yml", "extension-tests"), ("ci.yml", "lint-test")),
    "SCCACHE_CACHE_KEY": (("ci.yml", "typecheck-test"), ("ci.yml", "lint-test")),
    "TOOL_CACHE_KEY": (("ci.yml", "typecheck-test"),),
}
#: Keys naming the run rather than the content they hold. A compiler cache
#: depends on the source that was compiled, which no lockfile hash captures, so
#: a content-addressed key would hit forever and absorb nothing new. These
#: therefore carry no cache-hit guard on save: the key is new every run.
ROLLING_KEYS: typ.Final = ("SCCACHE_CACHE_KEY",)
#: Workflow-level values that render the tool cache key. A job restoring an
#: archive another workflow wrote can only hit while these agree.
SHARED_KEY_INPUTS: typ.Final = ("NEXTEST_VERSION", "CACHE_GENERATION", "UBUNTU_RELEASE")
KEY_SHARING_WORKFLOWS: typ.Final = ("ci.yml", "coverage-main.yml")


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


def _mapping(value: object, message: str) -> dict[str, object]:
    """Narrow a parsed YAML value to a string-keyed mapping.

    Returns
    -------
    dict[str, object]
        The narrowed mapping.
    """
    _require(
        condition=isinstance(value, dict)
        and all(isinstance(key, str) for key in value),
        message=message,
    )
    return typ.cast("dict[str, object]", value)


def workflow_document(workflow_name: str) -> dict[str, object]:
    """Parse one repository workflow.

    YAML 1.1 reads the ``on:`` trigger key as the boolean ``True``, so the
    document itself is not string-keyed and is returned without narrowing.

    Returns
    -------
    dict[str, object]
        The parsed workflow document.
    """
    path = WORKFLOW_DIR / workflow_name
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    _require(
        condition=isinstance(document, dict),
        message=f"{workflow_name} must parse to a mapping",
    )
    return typ.cast("dict[str, object]", document)


def workflow_env(workflow_name: str) -> dict[str, object]:
    """Return the workflow-level ``env`` mapping."""
    return _mapping(
        workflow_document(workflow_name).get("env"),
        f"{workflow_name} must declare workflow-level env",
    )


def jobs(workflow_name: str) -> dict[str, object]:
    """Load the jobs mapping from one repository workflow."""
    return _mapping(
        workflow_document(workflow_name).get("jobs"),
        f"{workflow_name} must declare jobs",
    )


def job(workflow_name: str, job_name: str) -> Job:
    """Return one named job from a repository workflow."""
    payload = _mapping(
        jobs(workflow_name).get(job_name),
        f"{workflow_name} must define {job_name}",
    )
    return typ.cast("Job", payload)


def steps(workflow_name: str, job_name: str) -> list[Step]:
    """Return the validated steps for one workflow job."""
    declared = job(workflow_name, job_name).get("steps")
    _require(
        condition=isinstance(declared, list),
        message=f"{workflow_name}:{job_name} must declare steps",
    )
    return [
        typ.cast(
            "Step",
            _mapping(
                step, f"{workflow_name}:{job_name} step {index} must be a mapping"
            ),
        )
        for index, step in enumerate(typ.cast("list[object]", declared))
    ]


def step_inputs(step: Step, message: str) -> dict[str, object]:
    """Return the ``with`` mapping declared by one workflow step."""
    return _mapping(step.get("with"), message)


def cache_paths(step: Step, message: str) -> list[str]:
    """Return the paths a cache step owns, one per line."""
    declared = step_inputs(step, message).get("path", "")
    _require(
        condition=isinstance(declared, str),
        message=f"{message}: paths must be a newline-delimited string",
    )
    return [line.strip() for line in str(declared).splitlines() if line.strip()]


def restore_steps(workflow_name: str, job_name: str) -> list[Step]:
    """Return the cache restore steps declared by one job, in order."""
    return [
        step
        for step in steps(workflow_name, job_name)
        if step.get("uses") == CACHE_RESTORE
    ]


def save_steps(workflow_name: str, job_name: str) -> list[Step]:
    """Return the cache save steps declared by one job, in order."""
    return [
        step
        for step in steps(workflow_name, job_name)
        if step.get("uses") == CACHE_SAVE
    ]


def cache_steps(workflow_name: str, job_name: str) -> list[Step]:
    """Return every step of one job that owns a cached path."""
    owners = {CACHE_RESTORE, CACHE_SAVE, CACHE_PLAIN}
    return [
        step for step in steps(workflow_name, job_name) if step.get("uses") in owners
    ]


def expand(manifest: cabc.Mapping[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    """Flatten a workflow-to-job-names manifest into per-job cases."""
    return [
        (workflow_name, job_name)
        for workflow_name, job_names in manifest.items()
        for job_name in job_names
    ]


def workflow_sources() -> list[tuple[str, str]]:
    """Return every workflow's name and source text."""
    return [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(WORKFLOW_DIR.glob("*.yml"))
    ]
