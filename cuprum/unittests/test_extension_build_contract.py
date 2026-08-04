"""Contract tests for the extension build wiring in the Makefile and CI.

`make test-extension` and the `extension-tests` job are the only things that
turn a silently-skipped extension into a failure, and both are configuration
rather than code. Nothing else in the suite notices when the guard variable
leaves the recipe, a module falls out of ``EXTENSION_TEST_TARGETS``, or the CI
job stops building the extension before running the gated tests: every one of
those changes leaves a green run behind. These tests read the wiring back and
assert the contract it must uphold.

The Makefile side is read through ``make --dry-run`` rather than by parsing
the file: that is the command line the target actually runs, with every
variable expanded, so an assertion about the guard variable is an assertion
about what CI executes rather than about text sitting near it.

``EXTENSION_TEST_TARGETS`` is pinned by two independent properties, neither
implying the other — ``_SKIP_SIGNALS`` derives the gated modules from the
suite itself so a *new* one cannot be forgotten, and ``_COMPANION_TARGETS``
names the modules that are in the job for reasons no scan can derive. What
each property does and does not catch is set out under "Building the
extension for tests" in docs/developers-guide.md; this module keeps a pointer
rather than repeating it.
"""

from __future__ import annotations

import functools
import os
import pathlib as pth
import re
import subprocess  # noqa: S404 - reads the repository's own Makefile recipes.
import typing as typ

import pytest
import yaml

from tests.helpers.docs import repo_root
from tests.helpers.extension_requirement import REQUIRE_EXTENSION_ENV

if typ.TYPE_CHECKING:
    import collections.abc as cabc

CI_WORKFLOW = ".github/workflows/ci.yml"

#: Where test modules live. Any of them could gate on the extension, so all
#: are scanned, not only the directories holding gated modules today.
_TEST_MODULE_GLOBS: typ.Final = (
    "cuprum/unittests/test_*.py",
    "tests/test_*.py",
    "tests/behaviour/test_*.py",
)

#: Textual signals that a module's outcome depends on the compiled extension,
#: each mapped to the reason it counts. Keep the reasons readable: they are
#: quoted verbatim in the failure that asks for a module to be added.
_SKIP_SIGNALS: typ.Final[dict[str, re.Pattern[str]]] = {
    "requests the root conftest's `rust_streams` fixture, which skips the "
    "test when the extension is absent": re.compile(r"\brust_streams\b"),
    "skips with the shared 'Rust extension is not installed' reason": re.compile(
        r"Rust extension is not installed"
    ),
    "names `cuprum._rust_backend_native`, so what it asserts depends on the "
    "real extension": re.compile(r"\b_rust_backend_native\b"),
}

#: Targets that no scan can derive, because they do not gate on the extension
#: at all, mapped to why the extension-tests job runs them anyway.
_COMPANION_TARGETS: typ.Final[dict[str, str]] = {
    "cuprum/unittests/test_extension_requirement_guard.py": (
        "the guard's own tests; running them in the guarded job is what "
        "proves the guard stays silent when the extension is present, rather "
        "than only that it fires when the extension is absent"
    ),
}


@functools.cache
def _dry_run(*goals: str) -> str:
    """Return what ``make --dry-run`` prints for `goals`, recipes expanded."""
    # MAKEFLAGS leaks in when pytest is itself launched from `make test`, and
    # an inherited jobserver flag makes this nested make warn about it.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"MAKEFLAGS", "MAKELEVEL", "MFLAGS"}
    }
    completed = subprocess.run(  # noqa: S603 - fixed argument vector.
        ["make", "--dry-run", *goals],  # noqa: S607 - `make` resolved from PATH.
        capture_output=True,
        check=True,
        cwd=repo_root(),
        env=environment,
        text=True,
    )
    return completed.stdout


def _sole_line_containing(text: str, needle: str, *, subject: str) -> str:
    """Return the single line of `text` holding `needle`, or fail naming it."""
    matches = [line for line in text.splitlines() if needle in line]
    assert len(matches) == 1, (
        f"expected exactly one {subject} line containing {needle!r}, found "
        f"{len(matches)}"
    )
    return matches[0]


@functools.cache
def _guarded_pytest_command() -> str:
    """Return the `test-extension` recipe line that sets the guard variable."""
    return _sole_line_containing(
        _dry_run("test-extension"),
        f"{REQUIRE_EXTENSION_ENV}=1",
        subject="test-extension recipe",
    )


@functools.cache
def _declared_targets() -> tuple[str, ...]:
    """Return the module paths `make test-extension` hands to pytest."""
    targets = tuple(
        word for word in _guarded_pytest_command().split() if word.endswith(".py")
    )
    assert targets, (
        "the guarded `make test-extension` command line names no test "
        "modules, so the job would run nothing"
    )
    return targets


@functools.cache
def _scanned_sources() -> dict[str, str]:
    """Return the source of every test module the signal scan considers.

    This module is excluded: it quotes every signal, so scanning it would
    match each one against its own definition.
    """
    root = repo_root()
    this_module = pth.Path(__file__).resolve().relative_to(root).as_posix()
    return {
        relative: path.read_text(encoding="utf-8")
        for glob in _TEST_MODULE_GLOBS
        for path in sorted(root.glob(glob))
        if (relative := path.relative_to(root).as_posix()) != this_module
    }


def _gated_modules() -> dict[str, str]:
    """Map each extension-gated test module to the signal that found it."""
    gated: dict[str, str] = {}
    for module, source in _scanned_sources().items():
        reason = next(
            (
                reason
                for reason, pattern in _SKIP_SIGNALS.items()
                if pattern.search(source)
            ),
            None,
        )
        if reason is not None:
            gated[module] = reason
    return gated


@functools.cache
def _workflow() -> dict[str, typ.Any]:
    """Parse the CI workflow."""
    parsed = yaml.safe_load((repo_root() / CI_WORKFLOW).read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{CI_WORKFLOW} must parse to a mapping"
    return parsed


def _job_steps(job_name: str) -> list[dict[str, typ.Any]]:
    """Return the steps of a named CI job."""
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict), f"{CI_WORKFLOW} must declare a jobs mapping"
    job = jobs.get(job_name)
    assert isinstance(job, dict), (
        f"{CI_WORKFLOW} must declare a {job_name!r} job; found {sorted(jobs)}"
    )
    steps = job.get("steps")
    assert isinstance(steps, list), f"the {job_name!r} job must declare steps"
    return steps


def _run_scripts() -> cabc.Iterator[tuple[str, str]]:
    """Yield the job name and script of every ``run:`` step in the workflow."""
    for job_name, job in (_workflow().get("jobs") or {}).items():
        for step in (job or {}).get("steps") or []:
            script = step.get("run")
            if isinstance(script, str):
                yield job_name, script


def _step_index_running(command: str, *, job_name: str) -> int:
    """Return the index of the first step in `job_name` running `command`."""
    for index, step in enumerate(_job_steps(job_name)):
        script = step.get("run")
        if isinstance(script, str) and command in script:
            return index
    pytest.fail(f"no step in the {job_name!r} job runs {command!r}")


def test_the_test_extension_recipe_sets_the_guard_variable() -> None:
    """`make test-extension` must require the extension, not merely run tests.

    Without the variable the recipe is an ordinary pytest run, which is the
    exact silent pass the target exists to prevent.
    """
    recipe = _dry_run("test-extension")

    assert f"{REQUIRE_EXTENSION_ENV}=1" in recipe, (
        f"the test-extension recipe must set {REQUIRE_EXTENSION_ENV}=1, or a "
        "run without the extension skips every gated module and still passes"
    )
    assert "uv run pytest" in _guarded_pytest_command(), (
        f"{REQUIRE_EXTENSION_ENV} must be set on the pytest invocation "
        "itself, not on some other command in the recipe"
    )


def test_every_declared_target_exists() -> None:
    """Each declared module must be on disk, so a rename cannot go stale."""
    root = repo_root()
    missing = [target for target in _declared_targets() if not (root / target).exists()]

    assert not missing, (
        "EXTENSION_TEST_TARGETS names modules that do not exist, so "
        f"`make test-extension` cannot collect them: {missing}"
    )


def test_every_extension_gated_module_is_a_declared_target() -> None:
    """A module gated on the extension must run in the guarded job.

    Outside the list it skips wherever it runs and is never reached by the one
    job that requires the extension, so it stops covering the boundary.
    """
    declared = set(_declared_targets())
    missing = {
        module: reason
        for module, reason in _gated_modules().items()
        if module not in declared
    }

    assert not missing, (
        "these test modules gate on the compiled extension but are not in "
        "the Makefile's EXTENSION_TEST_TARGETS, so nothing ever runs them "
        f"with the extension present: {missing}"
    )


def test_every_skip_signal_still_identifies_a_module() -> None:
    """Each signal must match something, or the derivation has gone dead.

    The check above passes trivially when the scan finds nothing, so a renamed
    fixture would otherwise empty it rather than fail it.
    """
    sources = _scanned_sources().values()
    dead = [
        reason
        for reason, pattern in _SKIP_SIGNALS.items()
        if not any(pattern.search(source) for source in sources)
    ]

    assert not dead, (
        "these extension-gating signals no longer match any test module, so "
        "they have stopped pinning anything; update them to the idiom now in "
        f"use: {dead}"
    )


@pytest.mark.parametrize(
    ("module", "reason"),
    [
        pytest.param(module, reason, id=module.rsplit("/", maxsplit=1)[-1])
        for module, reason in _COMPANION_TARGETS.items()
    ],
)
def test_the_always_run_companions_stay_declared(module: str, reason: str) -> None:
    """Modules in the job for reasons no scan can derive must stay listed."""
    assert module in set(_declared_targets()), (
        f"{module} must stay in EXTENSION_TEST_TARGETS: {reason}"
    )


def test_the_ci_job_builds_the_extension_before_running_the_gated_tests() -> None:
    """`extension-tests` must run `make develop` first.

    `make build` only syncs dependencies, so this ordering is the whole reason
    the job can pass at all, and nothing else asserts it.
    """
    build = _step_index_running("make develop", job_name="extension-tests")
    tests = _step_index_running("make test-extension", job_name="extension-tests")

    assert build < tests, (
        "the extension-tests job must build the extension with `make "
        "develop` before `make test-extension` runs; found the build at step "
        f"{build} and the tests at step {tests}"
    )


def test_the_develop_target_installs_pip_before_building() -> None:
    """`develop` must run `ensurepip` before `maturin develop`.

    maturin resolves its own script through the interpreter's `sysconfig`
    scheme, which needs pip present. Owning that ordering here is what lets
    every caller inherit it.
    """
    lines = _dry_run("develop").splitlines()
    ensurepip = next(
        index for index, line in enumerate(lines) if "ensurepip --upgrade" in line
    )
    develop = next(
        index for index, line in enumerate(lines) if "maturin develop" in line
    )

    assert ensurepip < develop, (
        "`make develop` must run `ensurepip --upgrade` before `maturin "
        "develop`, or maturin cannot resolve its own script"
    )


def test_the_develop_target_defaults_to_a_debug_build() -> None:
    """Builds run on every change stay unoptimized unless asked otherwise."""
    command = _sole_line_containing(
        _dry_run("develop"), "maturin develop", subject="develop recipe"
    )

    assert "--release" not in command, (
        "`make develop` must default to a debug build; MATURIN_DEVELOP_FLAGS "
        "is how a caller asks for an optimized one"
    )


def test_the_develop_target_forwards_the_release_flag() -> None:
    """`MATURIN_DEVELOP_FLAGS` must reach the `maturin develop` invocation.

    The benchmark ratchet shares this target instead of restating the build,
    and it can only do that if the flag it passes actually takes effect.
    """
    command = _sole_line_containing(
        _dry_run("develop", "MATURIN_DEVELOP_FLAGS=--release"),
        "maturin develop",
        subject="develop recipe",
    )

    assert "--release" in command, (
        "`make develop MATURIN_DEVELOP_FLAGS=--release` must pass --release "
        "through to maturin, or the benchmark ratchet measures a debug build"
    )


def test_the_benchmark_job_builds_through_the_develop_target() -> None:
    """`benchmark-ratchet` must reach an optimized build via `make develop`.

    Its numbers mean nothing against a debug build, so the flag matters as
    much as the shared target does.
    """
    index = _step_index_running("make develop", job_name="benchmark-ratchet")
    script = _job_steps("benchmark-ratchet")[index]["run"]

    assert "make develop MATURIN_DEVELOP_FLAGS=--release" in script, (
        "the benchmark-ratchet job must build with `make develop "
        "MATURIN_DEVELOP_FLAGS=--release`; without the flag the ratchet "
        "compares debug builds and its thresholds mean nothing"
    )


def test_no_ci_step_invokes_maturin_develop_directly() -> None:
    """`make develop` must be the only definition of the extension build.

    A second copy of the three-step sequence is how the two drift: the copy
    stops matching the target, and whichever job owns it quietly builds
    something nobody maintains.
    """
    offenders = sorted({
        job_name for job_name, script in _run_scripts() if "maturin develop" in script
    })

    assert not offenders, (
        "these CI jobs invoke `maturin develop` directly instead of going "
        f"through `make develop`: {offenders}"
    )
