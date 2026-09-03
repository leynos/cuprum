"""Shared manifests for the CI runner and cache contract tests.

The repository's Linux build and test jobs run on Ubicloud managed runners and
own every cache they depend on. Neither property is exercised by ordinary
tests: a workflow that silently moves back to a GitHub-hosted label, or grows a
second owner for `~/.cargo/registry`, still passes every functional suite. The
manifests below are therefore the declared intent, and the tests in
``tests/test_ci_runner_placement.py`` and ``tests/test_ci_cache_ownership.py``
read the workflows back against them through ``tests/helpers/ci_workflows.py``.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_workflows import (
    CACHE_ACTION_PIN,
    CACHE_PLAIN,
    CACHE_RESTORE,
    CACHE_SAVE,
    ROOT,
    WORKFLOW_DIR,
    cache_paths,
    cache_steps,
    expand,
    job,
    jobs,
    restore_steps,
    save_steps,
    step_inputs,
    steps,
    workflow_document,
    workflow_env,
    workflow_sources,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Re-exported so the manifests and the readers arrive from one import in every
# contract test. The split is by responsibility: this module declares what the
# workflows are meant to contain, `ci_workflows` reads what they do contain.
# fmt: off
__all__ = (
    "CACHE_ACTION_PIN", "CACHE_PLAIN", "CACHE_RESTORE", "CACHE_SAVE",
    "ROOT", "WORKFLOW_DIR", "cache_paths", "cache_steps", "expand", "job",
    "jobs", "restore_steps", "save_steps", "step_inputs", "steps",
    "workflow_document", "workflow_env", "workflow_sources",
)
# fmt: on

#: The one Ubicloud shape this repository uses. The recipe treats
#: `ubicloud-standard-4` as a ceiling reached only with measured evidence, and
#: no job here has produced that evidence.
UBICLOUD_LABEL = "ubicloud-standard-2"
#: vCPUs behind ``UBICLOUD_LABEL``. Test and build concurrency is bounded by
#: this number because the job is billed for exactly these cores.
UBICLOUD_VCPUS = 2
GITHUB_LABEL = "ubuntu-latest"

CACHE_KEYS_ACTION = "./.github/actions/cache-keys"
SCCACHE_ACTION = "./.github/actions/setup-sccache"
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
SHARED_KEY_INPUTS: typ.Final = ("CACHE_GENERATION", "UBUNTU_RELEASE")
KEY_SHARING_WORKFLOWS: typ.Final = ("ci.yml", "coverage-main.yml")
