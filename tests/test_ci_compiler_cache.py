"""Contract tests for the single compiler-output owner across every build shape.

This repository archives no `target` tree anywhere. sccache holds the objects a
`target` archive would preserve, and the archive key names the interpreter and
the build shape, so the debug, cranelift, release and coverage-instrumented
shapes coexist without any of them overwriting or starving another, and no job
needs a tree that is invalidated on every source change.

None of that is visible from a green run: a job whose `RUSTC_WRAPPER` never
reaches the compiler still passes while caching nothing, which is exactly what
run 33748907011 recorded for the lint gate.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.ci_leg_gate import ungated
from tests.helpers.ci_runners import (
    CACHE_ACTION_PIN,
    CACHE_KEYS_ACTION_FILE,
    CACHED_JOBS,
    FORBIDDEN_CACHE_PATHS,
    ROOT,
    SCCACHE_ACTION,
    SCCACHE_JOBS,
    SETUP_RUST,
    cache_paths,
    cache_steps,
    expand,
    step_inputs,
    steps,
    workflow_sources,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

KEY_RENDERER_SOURCE = ROOT / ".github" / "actions" / "cache-keys" / "action.yml"
SCCACHE_ACTION_SOURCE = ROOT / ".github" / "actions" / "setup-sccache" / "action.yml"
KEY_FAMILIES = ("CARGO_CACHE_KEY", "TOOL_CACHE_KEY", "SCCACHE_CACHE_KEY")
#: Steps that can invoke the compiler. The report must come after the last of
#: them, or the statistics describe a window that ends before the work they
#: claim to measure. Some entries also match steps that compile nothing, such
#: as `Check out repository`; that is harmless because only the last index is
#: used, and the lower bound is enforced by the reset's adjacency to setup.
MEASURED_STEP_PREFIXES = (
    "install",
    "build",
    "run",
    "generate",
    "prepare",
    "check",
    "validate",
    "lint",
)


@pytest.mark.parametrize(("workflow_name", "job_name"), SCCACHE_JOBS)
def test_every_rust_job_installs_the_wrapper_and_reports_its_counters(
    workflow_name: str, job_name: str
) -> None:
    """Zero the counters before the work and publish them after, without masking."""
    job_steps = steps(workflow_name, job_name)
    setup_indices = [
        index
        for index, step in enumerate(job_steps)
        if step.get("uses") == SCCACHE_ACTION
    ]
    assert len(setup_indices) == 1, (
        f"{workflow_name}:{job_name} compiles Rust, so it must install the "
        "checksum-verified wrapper exactly once"
    )
    reset_index = next(
        index
        for index, step in enumerate(job_steps)
        if step.get("name") == "Reset compiler-cache counters"
    )
    stats_index = next(
        index
        for index, step in enumerate(job_steps)
        if step.get("name") == "Record compiler-cache effectiveness"
    )
    # Immediately after setup, not merely somewhere before the report. Any step
    # in between may compile: `lint-test` builds the cranelift Whitaker suite
    # while installing it, and those requests were zeroed away before the
    # report while a weaker ordering assertion still passed.
    assert reset_index == setup_indices[0] + 1, (
        f"{workflow_name}:{job_name} must zero the counters in the step "
        f"immediately after installing the wrapper, so no compilation escapes "
        f"the measured window; reset is at {reset_index}, setup at "
        f"{setup_indices[0]}"
    )
    measured = [
        index
        for index, step in enumerate(job_steps)
        if str(step.get("name", "")).lower().startswith(MEASURED_STEP_PREFIXES)
    ]
    assert measured, f"{workflow_name}:{job_name} must do some measurable work"
    # The lower bound is already covered, and more strictly, by the adjacency
    # assertion above: nothing at all sits between the wrapper and the reset.
    # This is the upper bound, so no compilation happens after the report.
    assert stats_index > max(measured), (
        f"{workflow_name}:{job_name} must report the counters after "
        f"{job_steps[max(measured)].get('name')!r}, the last step that can "
        "invoke the compiler"
    )
    stats = job_steps[stats_index]
    assert ungated(workflow_name, job_name, stats.get("if")) == "always()", (
        f"{workflow_name}:{job_name} must report the counters even when the build fails"
    )
    script = stats.get("run")
    assert isinstance(script, str), (
        f"{workflow_name}:{job_name} compiler-cache report must run a script"
    )
    assert "|| true" not in script, (
        f"{workflow_name}:{job_name} must not mask a failing compiler-cache probe"
    )
    assert "--stats-format json" in script, (
        f"{workflow_name}:{job_name} must record machine-readable stats"
    )


def test_sccache_still_offers_a_capped_caller_owned_directory() -> None:
    """Keep the directory backend usable, and keep its cap.

    This rule once forbade `SCCACHE_GHA_ENABLED` outright, on the ground that
    the Actions backend writes to GitHub's cache rather than Ubicloud's, where
    it competes with the Windows and macOS lanes for the per-repository quota.
    That is still true of a GitHub-hosted lane, which is why `local` is the
    action's default and why `lint-test` and `loom` keep it. It was never true
    of an Ubicloud lane that exports the proxy credentials first, where the
    Actions cache service is Ubicloud's own proxy;
    `test_ci_compiler_cache_backend` holds that distinction and the ordering
    it depends on.

    What survives unchanged is the directory arm itself. Its archive grows with
    every new compilation unit until the cap trims it, and the cap holds two
    build shapes, the lint and test objects and the instrumented ones, so a
    one-shape cap would evict each shape in turn.
    """
    text = SCCACHE_ACTION_SOURCE.read_text(encoding="utf-8")
    required = ("SCCACHE_DIR=", "SCCACHE_CACHE_SIZE=", "default: 4G")
    missing = [fragment for fragment in required if fragment not in text]
    assert not missing, (
        f"the sccache action must declare a capped cache directory; missing {missing}"
    )


def test_shared_rust_setup_owns_no_cache_of_its_own() -> None:
    """Leave one owner per path: this workflow's steps, not the shared action's.

    Selecting `external` delegates cache ownership to the workflow. The lint
    job temporarily selects a second toolchain because Nixie's Merman
    dependency needs newer Rust than the project gate. Its Nixie, formatter
    nightly, and restored project-stable setup steps must keep that same
    external cache policy.
    """
    for workflow_name, job_name in expand(CACHED_JOBS):
        setup_steps = [
            step
            for step in steps(workflow_name, job_name)
            if step.get("uses") == SETUP_RUST
        ]
        if not setup_steps:
            continue
        expected_count = (
            3 if (workflow_name, job_name) == ("ci.yml", "lint-test") else 1
        )
        assert len(setup_steps) == expected_count, (
            f"{workflow_name}:{job_name} must use shared setup-rust "
            f"{expected_count} time(s)"
        )
        for setup_step in setup_steps:
            inputs = step_inputs(
                setup_step, f"{workflow_name}:{job_name} setup-rust inputs"
            )
            assert inputs.get("cache-provider") == "external", (
                f"{workflow_name}:{job_name} must delegate cache ownership to the "
                "workflow"
            )
            assert inputs.get("use-sccache") == "false", (
                f"{workflow_name}:{job_name} must disable the shared compiler cache"
            )


def test_every_key_names_the_runner_lane_that_wrote_it() -> None:
    """Let one key name serve both lanes without either restoring the other."""
    text = KEY_RENDERER_SOURCE.read_text(encoding="utf-8")
    assert "RUNNER_ENVIRONMENT" in text, (
        "every rendered key must carry runner.environment, so a GitHub-hosted "
        "archive can never be restored onto an Ubicloud runner or the reverse"
    )
    for name in KEY_FAMILIES:
        assert name in text, f"{name} must be rendered by the shared action"


def _declared_cache_paths() -> cabc.Iterator[tuple[str, str, str]]:
    """Yield the workflow, job, and path of every cached path in the estate."""
    for workflow_name, job_name in expand(CACHED_JOBS):
        for step in cache_steps(workflow_name, job_name):
            message = f"{workflow_name}:{job_name} cache step"
            for path in cache_paths(step, message):
                yield workflow_name, job_name, path


def test_no_cache_step_owns_a_target_directory() -> None:
    """Leave compiler output to sccache; a `target` archive has no owner here."""
    offenders = [
        f"{workflow_name}:{job_name} archives {path}"
        for workflow_name, job_name, path in _declared_cache_paths()
        if path in FORBIDDEN_CACHE_PATHS
    ]
    assert not offenders, f"no job may archive a target tree: {offenders}"


def test_no_workflow_lists_a_target_path_at_all() -> None:
    """Catch a target path added to a cache step this manifest does not know."""
    offenders = [
        f"{workflow_name}: {line.strip()}"
        for workflow_name, source in workflow_sources()
        for line in source.splitlines()
        if line.strip() in FORBIDDEN_CACHE_PATHS
    ]
    assert not offenders, f"no workflow may name a target tree as a path: {offenders}"


def test_every_lane_pins_the_intercepted_cache_action() -> None:
    """Use the version Ubicloud's transparent cache actually intercepts."""
    for workflow_name, source in workflow_sources():
        for line in source.splitlines():
            stripped = line.strip()
            if "actions/cache@" in stripped or "actions/cache/" in stripped:
                assert CACHE_ACTION_PIN in stripped, (
                    f"{workflow_name} pins a cache action Ubicloud does not "
                    f"intercept: {stripped}"
                )
        assert "ubicloud/cache" not in source, (
            f"{workflow_name} must not use the deprecated ubicloud/cache fork"
        )


def test_no_matrix_step_reads_a_retired_leg_flag() -> None:
    """A guard on `matrix.python-suite` would now switch its step off.

    The matrix used to carry `python-suite` so its typecheck-only 3.13 leg
    could skip the compiler cache. That leg is gone and so is the key, and an
    expression reading a key no leg declares renders empty, which is falsy: a
    leftover or re-added guard would silently skip the wrapper, the tests, or
    the save on every leg while the job stayed green.
    """
    stale = [
        str(step.get("name", ""))
        for step in steps("ci.yml", "typecheck-test")
        if "python-suite" in str(step.get("if", ""))
    ]
    assert not stale, f"ci.yml:typecheck-test steps gated on a retired flag: {stale}"


def test_the_compiler_cache_is_written_by_a_job_that_compiles() -> None:
    """Keep the rolling generation from freezing.

    A leg that compiles nothing would restore the previous generation and
    republish it unchanged for ever, so the cache would never absorb a new
    object while still reporting hits. Every remaining matrix leg runs the
    Python suite and compiles, so its save carries the trunk guard alone.
    """
    writers = [
        step
        for workflow_name, job_name in expand(CACHED_JOBS)
        for step in steps(workflow_name, job_name)
        if str(step.get("name", "")) == "Save the compiler cache"
    ]
    assert writers, "some job must publish the compiler cache on trunk"
    matrix_writers = [
        step
        for step in steps("ci.yml", "typecheck-test")
        if str(step.get("name", "")) == "Save the compiler cache"
    ]
    assert len(matrix_writers) == 1, (
        "each interpreter leg owns the compiler-cache family for its own "
        "interpreter, so the matrix declares exactly one save step"
    )
    # Compared whole, not by containment, so a widened or narrowed guard
    # fails either way.
    condition = ungated("ci.yml", "typecheck-test", matrix_writers[0].get("if"))
    expected = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert condition == expected, (
        f"the matrix save must carry if: {expected!r}, got {condition!r}"
    )


def test_the_compiler_cache_key_names_the_interpreter_and_the_build_shape() -> None:
    """Keep objects that cannot serve each other in separate archives.

    Measured on 2026-09-04, before the key carried either component: one
    instrumented Python 3.13 archive served every Ubicloud job, the 3.13 reader
    took 14 of its 17 cacheable compiles, and the 3.12, 3.14 and 3.15a readers
    took none at all. Even with `pyo3` on the stable ABI, its build script
    rebuilds for each interpreter, so an object compiled under one CPython does
    not serve another, and an optimized or instrumented object is useless to an
    unoptimized build.
    """
    source = CACHE_KEYS_ACTION_FILE.read_text(encoding="utf-8")
    prefix = next(line for line in source.splitlines() if "sccache_prefix=" in line)
    for component in ("${PYTHON_VERSION}", "${COMPILER_SHAPE}"):
        assert component in prefix, (
            f"the compiler-cache prefix must carry {component}; got {prefix.strip()!r}"
        )


def test_pyo3_targets_the_stable_abi_but_keeps_per_interpreter_objects() -> None:
    """Anchor the reason the interpreter is still in the key to the manifest.

    `pyo3` builds against the stable ABI from CPython 3.12 (`abi3-py312`), so
    one wheel serves every supported interpreter. Compiler output is still
    per-interpreter: measured on 2026-09-24, switching `maturin build -i` from
    3.13 to 3.12 recompiled `pyo3-ffi`, `pyo3`, and the extension, because the
    pyo3 build script records the interpreter's configuration file and rebuilds
    when it changes. Keep the per-interpreter families until a CI measurement
    shows that archives written under one interpreter serve another.
    """
    manifest = (ROOT / "rust" / "cuprum-rust" / "Cargo.toml").read_text(
        encoding="utf-8"
    )
    pyo3 = next(line for line in manifest.splitlines() if line.startswith("pyo3"))
    assert "abi3-py312" in pyo3, (
        "pyo3 no longer targets the CPython 3.12 stable ABI, so native wheels "
        "would cover a single interpreter again; restore the abi3-py312 feature"
    )
