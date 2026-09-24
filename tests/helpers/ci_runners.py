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

from tests.helpers.ci_job_rules import ceiling, never_runs, references
from tests.helpers.ci_placement import (
    FORK_FIELD,
    FROZEN_HOSTED_LABELS,
    Placement,
    all_jobs,
    declares_steps,
    placement,
)
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
    job_env,
    jobs,
    restore_steps,
    save_steps,
    single_step_position_using,
    single_step_using,
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
    "CACHE_ACTION_PIN",
    "CACHE_PLAIN",
    "CACHE_RESTORE",
    "CACHE_SAVE",
    "FORK_FIELD",
    "FROZEN_HOSTED_LABELS",
    "ROOT",
    "RUNNER_NAMED_JOBS",
    "WORKFLOW_DIR",
    "Placement",
    "all_jobs",
    "cache_paths",
    "cache_steps",
    "ceiling",
    "declares_steps",
    "expand",
    "job",
    "job_env",
    "jobs",
    "never_runs",
    "placement",
    "references",
    "restore_steps",
    "save_steps",
    "single_step_position_using",
    "single_step_using",
    "step_inputs",
    "steps",
    "workflow_document",
    "workflow_env",
    "workflow_sources",
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
WINDOWS_LABEL = "windows-2022"

CACHE_KEYS_ACTION = "./.github/actions/cache-keys"
CACHE_KEYS_ACTION_FILE = ROOT / ".github" / "actions" / "cache-keys" / "action.yml"
SCCACHE_ACTION = "./.github/actions/setup-sccache"
SCCACHE_ACTION_FILE = ROOT / ".github" / "actions" / "setup-sccache" / "action.yml"
#: The shared action that republishes Ubicloud's cache-proxy credentials
#: through `GITHUB_ENV`. A `run:` step never sees `ACTIONS_CACHE_URL`, so
#: without this an sccache server binds local disk for the whole job.
CREDENTIALS_ACTION = (
    "leynos/shared-actions/.github/actions/export-ubicloud-cache-credentials"
)
CREDENTIALS_STEP = "Export the Ubicloud cache credentials"
#: Jobs that bind sccache to the Actions cache service rather than to a
#: directory this repository archives. On Ubicloud that service is Ubicloud's
#: own proxy, which these lanes read and write directly, so no job publishes a
#: generation for them. The store is branch scoped under Ubicloud's default
#: protection: pull requests read what `coverage-upload` writes on `main`.
GHA_BACKEND_JOBS: typ.Final = (
    ("ci.yml", "coverage"),
    ("coverage-main.yml", "coverage-upload"),
)
SETUP_RUST = (
    "leynos/shared-actions/.github/actions/setup-rust@"
    "c5a54701c8603a0fa756a6b34c49bc2af75a6c11"
)
#: The pinned shared coverage action. It drops `target` from its own cache, so
#: the no-target-archive rule holds even if a caller ever switches back to
#: `cache-provider: github`; it adds the `all-features`, `all-targets`, and
#: `doctests` inputs the coverage jobs depend on; and it installs cargo-nextest
#: from checksummed official release archives with no source-build fallback,
#: which matters because the coverage job is now the only place nextest runs.
#:
#: It is deliberately ahead of `SETUP_RUST` for now. From this revision the
#: ratchet baseline is published only on a push to `refs/heads/main` unless
#: `publish-baseline` says otherwise, and cuprum needs that: without it a pull
#: request advances the baseline it is then measured against. Holding one SHA
#: across the estate is still the aim, and the next estate-wide bump should
#: bring `SETUP_RUST` up to meet this one rather than pulling this one back.
#:
#: Asserted by value rather than by shape, unlike the Dependabot-owned pins in
#: `test_workflow_contract.py`. A bump has to update this constant, which is
#: the point: it makes someone confirm the new revision still keeps a pull
#: request from publishing.
GENERATE_COVERAGE = (
    "leynos/shared-actions/.github/actions/generate-coverage@"
    "77ea10341249024e22ec5d9069e3caa7596e0d4f"
)
OBSERVATION_STEP = "Record cache observations"

#: Repository-owned Linux build and test jobs. Every one is developer-blocking
#: and does real work, which is what buys it a paid runner.
UBICLOUD_JOBS: typ.Final[cabc.Mapping[str, tuple[str, ...]]] = {
    "build-wheels.yml": ("build-pure-wheel", "verify-wheel-install"),
    "ci.yml": (
        "lint-test",
        "typecheck-test",
        "extension-tests",
        "coverage",
        "benchmark-ratchet",
        "changes",
    ),
    "coverage-main.yml": ("coverage-upload",),
}
#: Ubicloud lanes a pull request from a fork can reach, which must therefore
#: declare the fallback arm. Derived intent, not derived fact: the workflows
#: are read back against it, and `test_fork_reachability_matches_the_manifest`
#: holds it against the triggers so a lane cannot quietly leave the set.
#:
#: `coverage-upload` is absent because `coverage-main.yml` triggers only on a
#: push to `main` and a dispatch, neither of which a fork can cause. The two
#: `build-wheels.yml` jobs are present because `ci.yml` calls that workflow on
#: every pull request, so its own `workflow_call` trigger understates its
#: exposure (weaver: a called workflow's triggers are its callers').
FORK_REACHABLE_UBICLOUD_JOBS: typ.Final[cabc.Mapping[str, tuple[str, ...]]] = {
    "build-wheels.yml": ("build-pure-wheel", "verify-wheel-install"),
    "ci.yml": (
        "lint-test",
        "typecheck-test",
        "extension-tests",
        "coverage",
        "benchmark-ratchet",
        "changes",
    ),
}
#: The one job permitted to fail without failing the workflow, and the matrix
#: key that says so. The 3.15a leg tracks a pre-release interpreter and is not
#: a required context; every other leg gates a merge.
EXPERIMENTAL_LEG_KEY = "experimental"
CONTINUE_ON_ERROR_JOBS: typ.Final = (("ci.yml", "typecheck-test"),)
#: Jobs that stay on GitHub-hosted runners, and why. Ubicloud offers Linux
#: only, and a job that sleeps, calls an API, or publishes an artefact someone
#: else built gains nothing from a metered build slot.
GITHUB_HOSTED_JOBS: typ.Final[cabc.Mapping[str, tuple[str, ...]]] = {
    # `lint-test` moved to the Ubicloud manifest: it is a developer-blocking
    # Linux gate that compiles, which is what buys a paid runner. `changes`
    # followed because it gates the required `benchmark-ratchet` and a hosted
    # queue held whole pull requests behind it. `loom-smoke` and
    # `workflow-harness` gate no required check and stay here, and
    # `rust-boundaries.yml`'s verifier lanes stay by this repository's own
    # decision recorded below.
    "ci.yml": ("loom-smoke",),
    "benchmark-gate-harness.yml": ("workflow-harness",),
    "delayed-pr-comment.yml": ("delay_and_comment",),
    "loom.yml": ("loom",),
    "release.yml": ("publish",),
    # Issue379 requires verifier schedules on GitHub-hosted Linux.
    "rust-boundaries.yml": ("verus", "extended"),
}
#: Matrix jobs whose check context spells the runner label, reviewed and
#: accepted. A name reading a matrix key is stable per event, which is the
#: property the stability rule protects, but the context still carries a label
#: and a placement change would rename it. Two of `build-native-wheels`'
#: contexts are named verbatim in the `main-required-checks` ruleset, which is
#: why moving its Linux legs is the repository owner's decision rather than a
#: label change; `native` is not a required context today.
RUNNER_NAMED_JOBS: typ.Final = (("rust-boundaries.yml", "native"),)

#: Windows-native validation needs GitHub's hosted Windows image; Ubicloud
#: offers Linux capacity only.
WINDOWS_HOSTED_JOBS: typ.Final[cabc.Mapping[str, tuple[str, ...]]] = {
    "ci.yml": ("extension-tests-windows",),
}
#: Jobs that restore at least one cache through the shared renderer.
CACHED_JOBS: typ.Final[cabc.Mapping[str, tuple[str, ...]]] = {
    "ci.yml": (
        "lint-test",
        "typecheck-test",
        "extension-tests",
        "coverage",
        "benchmark-ratchet",
        "loom-smoke",
    ),
    "coverage-main.yml": ("coverage-upload",),
    "loom.yml": ("loom",),
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
#: `typecheck-test` is absent: one of its legs only typechecks, so the wrapper
#: is installed conditionally there and a job-wide contract cannot describe it.
#: `test_the_typecheck_only_leg_installs_no_wrapper` covers that case instead.
SCCACHE_JOBS: typ.Final = (
    ("rust-boundaries.yml", "verus"),
    ("ci.yml", "lint-test"),
    ("ci.yml", "extension-tests"),
    ("ci.yml", "coverage"),
    ("ci.yml", "benchmark-ratchet"),
    ("coverage-main.yml", "coverage-upload"),
    ("loom.yml", "loom"),
)
#: Steps in the interpreter matrix that must follow the Python suite, because
#: without it the job compiles nothing and the wrapper would report zero
#: requests, which reads as a broken integration rather than as no work.
#: Each entry pairs a step name with the exact condition it must carry. The
#: report keeps its `always()` so a failed build still says what it compiled.
SUITE_GATED_STEPS: typ.Final = (
    ("Restore the compiler cache", "matrix.python-suite"),
    ("Set up sccache", "matrix.python-suite"),
    ("Reset compiler-cache counters", "matrix.python-suite"),
    ("Record compiler-cache effectiveness", "always() && matrix.python-suite"),
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
    # `lint-test` restores this key and no longer saves it. On its owned arm
    # it renders the `self-hosted` lane, which is the family `extension-tests`
    # writes, and the registry holds the resolved dependency graph either job
    # would have archived.
    "CARGO_CACHE_KEY": (("ci.yml", "extension-tests"),),
    # The compiler cache is written by whichever job actually compiles, and
    # each compile shape is its own family. See CACHE_FAMILY_WRITERS: this
    # mapping only says which jobs hold a save step, not which archive each
    # one publishes.
    # `coverage-main.yml:coverage-upload` is deliberately absent, as is
    # ci.yml's `coverage`. Both run sccache against Ubicloud's cache proxy,
    # reading and writing the store directly, so there is no archive
    # generation for a job to publish. The store stays branch scoped under
    # Ubicloud's default protection; see docs/ci-cache-ownership.md.
    "SCCACHE_CACHE_KEY": (
        ("ci.yml", "benchmark-ratchet"),
        ("ci.yml", "extension-tests"),
        ("ci.yml", "lint-test"),
        ("ci.yml", "typecheck-test"),
        ("loom.yml", "loom"),
    ),
    "TOOL_CACHE_KEY": (("ci.yml", "typecheck-test"),),
}
#: One writer per rendered family, which is the invariant that actually
#: matters: five jobs name ``SCCACHE_CACHE_KEY`` and publish five disjoint
#: archives, because the rendered key carries the lane, the interpreter and the
#: build shape. Each entry is
#: ``(key, lane, scope...) -> (workflow, job)``.
#:
#: The compiler split was measured on 2026-09-04. Before it, one instrumented
#: 3.13 archive served every Ubicloud job: the 3.13 reader took 14 of its 17
#: cacheable compiles and the 3.12, 3.14 and 3.15a readers took none, because
#: `pyo3` is declared without `abi3` and an extension compiled against one
#: CPython serves no other. `benchmark-ratchet` builds with `--release` and the
#: coverage jobs build under instrumentation, so neither can share an archive
#: with an unoptimized build either.
#:
#: The typecheck-only leg is absent by construction: it compiles nothing, its
#: save step is gated on ``matrix.python-suite``, and `extension-tests` owns
#: the 3.13 unoptimized family instead.
CACHE_FAMILY_WRITERS: typ.Final[
    cabc.Mapping[tuple[str, str, tuple[str, ...]], tuple[str, str]]
] = {
    ("CARGO_CACHE_KEY", "self-hosted", ()): ("ci.yml", "extension-tests"),
    ("TOOL_CACHE_KEY", "self-hosted", ("3.12",)): ("ci.yml", "typecheck-test"),
    ("TOOL_CACHE_KEY", "self-hosted", ("3.13",)): ("ci.yml", "typecheck-test"),
    ("TOOL_CACHE_KEY", "self-hosted", ("3.14",)): ("ci.yml", "typecheck-test"),
    ("TOOL_CACHE_KEY", "self-hosted", ("3.15",)): ("ci.yml", "typecheck-test"),
    ("SCCACHE_CACHE_KEY", "self-hosted", ("3.13", "lint")): (
        "ci.yml",
        "lint-test",
    ),
    ("SCCACHE_CACHE_KEY", "self-hosted", ("3.12", "debug")): (
        "ci.yml",
        "typecheck-test",
    ),
    ("SCCACHE_CACHE_KEY", "self-hosted", ("3.13", "debug")): (
        "ci.yml",
        "extension-tests",
    ),
    ("SCCACHE_CACHE_KEY", "self-hosted", ("3.14", "debug")): (
        "ci.yml",
        "typecheck-test",
    ),
    ("SCCACHE_CACHE_KEY", "self-hosted", ("3.15", "debug")): (
        "ci.yml",
        "typecheck-test",
    ),
    ("SCCACHE_CACHE_KEY", "self-hosted", ("3.13", "release")): (
        "ci.yml",
        "benchmark-ratchet",
    ),
    ("SCCACHE_CACHE_KEY", "github-hosted", ("3.13", "loom")): (
        "loom.yml",
        "loom",
    ),
}
#: Keys naming the run rather than the content they hold. A compiler cache
#: depends on the source that was compiled, which no lockfile hash captures, so
#: a content-addressed key would hit forever and absorb nothing new. These
#: therefore carry no cache-hit guard on save: the key is new every run.
ROLLING_KEYS: typ.Final = ("SCCACHE_CACHE_KEY",)
#: Workflow-level values that render the tool cache key. A job restoring an
#: archive another workflow wrote can only hit while these agree.
SHARED_KEY_INPUTS: typ.Final = ("CACHE_GENERATION", "UBUNTU_RELEASE")
KEY_SHARING_WORKFLOWS: typ.Final = ("ci.yml", "coverage-main.yml", "loom.yml")
