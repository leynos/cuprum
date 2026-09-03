"""Contract tests for the single compiler-output owner across every build shape.

This repository archives no `target` tree anywhere. sccache holds the objects a
`target` archive would preserve, keyed by the flags that distinguish the debug,
cranelift, and coverage-instrumented shapes, so all three coexist in one store
and no job needs a tree that is invalidated on every source change.

None of that is visible from a green run: a job whose `RUSTC_WRAPPER` never
reaches the compiler still passes while caching nothing, which is exactly what
run 33748907011 recorded for the lint gate.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.ci_runners import (
    CACHE_ACTION_PIN,
    CACHED_JOBS,
    FORBIDDEN_CACHE_PATHS,
    ROOT,
    SCCACHE_ACTION,
    SCCACHE_JOBS,
    SETUP_RUST,
    SUITE_GATED_STEPS,
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
    assert stats.get("if") == "always()", (
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


def test_sccache_uses_a_caller_owned_directory_not_the_github_backend() -> None:
    """Send compiler objects to the store the workflow can list, not GitHub's."""
    text = SCCACHE_ACTION_SOURCE.read_text(encoding="utf-8")
    assert "SCCACHE_GHA_ENABLED" not in text, (
        "the GitHub Actions backend writes to GitHub's cache rather than "
        "Ubicloud's, where it competes with the Windows and macOS lanes"
    )
    # A capped directory, sized for two build shapes: the lint and test
    # objects, and the coverage-instrumented objects.
    required = ("SCCACHE_DIR=", "SCCACHE_CACHE_SIZE=", "default: 4G")
    missing = [fragment for fragment in required if fragment not in text]
    assert not missing, (
        f"the sccache action must declare a capped cache directory; missing {missing}"
    )


def test_shared_rust_setup_owns_no_cache_of_its_own() -> None:
    """Leave one owner per path: this workflow's steps, not the shared action's.

    The shared action's `actions/cache` step covers `target/${BUILD_PROFILE}`
    as well as the registry, and it is gated on `cache-provider: github`.
    Selecting `external` is what disables the target archive.
    """
    for workflow_name, job_name in expand(CACHED_JOBS):
        setup_steps = [
            step
            for step in steps(workflow_name, job_name)
            if step.get("uses") == SETUP_RUST
        ]
        if not setup_steps:
            continue
        assert len(setup_steps) == 1, (
            f"{workflow_name}:{job_name} must use shared setup-rust once"
        )
        inputs = step_inputs(
            setup_steps[0], f"{workflow_name}:{job_name} setup-rust inputs"
        )
        assert inputs.get("cache-provider") == "external", (
            f"{workflow_name}:{job_name} must delegate cache ownership to the "
            "caller, which is also what disables the shared target archive"
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


def test_the_typecheck_only_leg_installs_no_wrapper() -> None:
    """Skip the compiler cache on the leg that compiles nothing.

    A job that installs sccache and reports zero compile requests is
    indistinguishable in the log from one whose wrapper never reached the
    compiler, which is a failure this repository has already had. The leg that
    only typechecks therefore reports nothing rather than zero.
    """
    job_steps = steps("ci.yml", "typecheck-test")
    by_name = {str(step.get("name", "")): step for step in job_steps}
    for name in SUITE_GATED_STEPS:
        assert name in by_name, f"ci.yml:typecheck-test must declare {name!r}"
        condition = " ".join(str(by_name[name].get("if", "")).split())
        assert "matrix.python-suite" in condition, (
            f"ci.yml:typecheck-test step {name!r} must follow the Python "
            f"suite, got if: {condition!r}"
        )


def test_the_compiler_cache_is_written_by_a_job_that_compiles() -> None:
    """Keep the rolling generation from freezing.

    The writer used to be the interpreter leg that the deduplication left with
    only a typechecker. It would have restored the previous generation and
    republished it unchanged for ever, so the cache would never absorb a new
    object while still reporting hits.
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
    assert not matrix_writers, (
        "the interpreter matrix must not own the compiler cache: its legs "
        "either typecheck only or compile a fraction of what the coverage job "
        "does, so the generation they publish would go stale"
    )
