"""Exercise how the Makefile hands Cargo its job count.

The Makefile once pinned Cargo to one job, which starved a billed runner and
overrode a caller's `CARGO_BUILD_JOBS`. These tests drive Make itself, with
inert tools, rather than read its text: they check the commands Make would run
on both the `cargo-nextest` path and the `cargo test` fallback, and that a
caller's `CARGO_BUILD_JOBS` reaches the recipe environment unchanged.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - controlled Make argv.

import pytest

from tests.helpers.docs import repo_root

_RECIPE_TARGETS = ("test-rust", "test-python", "lint-clippy", "lint-whitaker")


def _make(*arguments: str, caller_jobs: str | None = None) -> str:
    """Return the output of one Make invocation with inert tools."""
    executable = shutil.which("make")
    assert executable is not None, "the Makefile contract tests require GNU Make"
    env = {k: v for k, v in os.environ.items() if k != "CARGO_BUILD_JOBS"}
    env["MAKEFLAGS"] = ""
    if caller_jobs is not None:
        env["CARGO_BUILD_JOBS"] = caller_jobs
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [executable, "CARGO=probe-cargo", "WHITAKER=probe-whitaker", *arguments],
        capture_output=True,
        check=True,
        cwd=repo_root(),
        env=env,
        text=True,
    )
    return completed.stdout


def _recipe(target: str, *variables: str) -> str:
    """Return the commands Make would run for ``target``."""
    return _make("--dry-run", *variables, target)


@pytest.mark.parametrize("target", _RECIPE_TARGETS)
def test_no_recipe_pins_cargo_to_a_job_count(target: str) -> None:
    """No Rust or pytest recipe sets `--jobs 1` or `CARGO_BUILD_JOBS` itself."""
    recipe = _recipe(target)

    assert "CARGO_BUILD_JOBS" not in recipe, f"{target} must not set CARGO_BUILD_JOBS"
    assert "--jobs" not in recipe, f"{target} must not pass --jobs to Cargo"


def test_the_nextest_path_takes_test_threads_not_a_build_limit() -> None:
    """`TEST_JOBS` reaches nextest as test threads and nowhere as build jobs."""
    recipe = _recipe("test-rust", "TEST_JOBS=3")
    nextest_line = next(line for line in recipe.splitlines() if " nextest run " in line)

    assert "--test-threads 3" in nextest_line, "nextest must take TEST_JOBS as threads"
    assert "--jobs" not in nextest_line, "nextest must not be given --jobs"


@pytest.mark.parametrize(
    "cargo_flags",
    [
        pytest.param(None, id="default-flags"),
        pytest.param("CARGO_FLAGS=--all-features", id="override-without-all-targets"),
    ],
)
def test_the_cargo_test_fallback_leaves_build_jobs_to_the_caller(
    cargo_flags: str | None,
) -> None:
    """The fallback caps test threads after `--`, never Cargo's build jobs.

    The override case drops `--all-targets`, which a matcher keyed on the
    default flags would miss, so it guards the anchoring on stable tokens.
    """
    recipe = _recipe(
        "test-rust", "TEST_JOBS=3", *([cargo_flags] if cargo_flags else [])
    )
    # Anchor on the `test` subcommand and the thread cap, not on `CARGO_FLAGS`,
    # which a caller may override.
    fallback_line = next(
        (
            line
            for line in recipe.splitlines()
            if " test " in line and "--test-threads=" in line and "nextest" not in line
        ),
        None,
    )
    assert fallback_line is not None, (
        "the cargo test fallback must cap test threads with --test-threads="
    )
    before, separator, after = fallback_line.partition(" -- ")

    assert separator == " -- ", "the fallback must forward test options after `--`"
    assert "--test-threads=3" in after, "the fallback must cap test threads"
    assert "--jobs" not in before, "the fallback must not cap Cargo's build jobs"


def test_a_caller_cargo_build_jobs_reaches_the_recipe_environment() -> None:
    """The Makefile neither defines nor overrides the caller's value."""
    database = _make("--dry-run", "--print-data-base", "test-rust", caller_jobs="7")
    lines = database.splitlines()
    index = next(i for i, line in enumerate(lines) if line == "CARGO_BUILD_JOBS = 7")

    assert lines[index - 1] == "# environment", (
        "CARGO_BUILD_JOBS must keep the caller's environment value; the Makefile "
        f"must not redefine it (origin: {lines[index - 1]!r})"
    )


def test_cargo_build_jobs_is_undefined_when_the_caller_sets_none() -> None:
    """Without a caller value the Makefile supplies none: Cargo's default holds."""
    database = _make("--dry-run", "--print-data-base", "test-rust")

    assert not any(
        line.startswith("CARGO_BUILD_JOBS") for line in database.splitlines()
    ), "the Makefile must not define CARGO_BUILD_JOBS on its own"
