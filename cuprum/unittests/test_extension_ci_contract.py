"""Contract tests for how CI builds the extension before testing against it.

`make develop` is the one definition of the extension build, and nothing else
in the suite notices when a CI job stops going through it: remove the build
step from `extension-tests` and the gated modules quietly skip, drop
`--release` from `benchmark-ratchet` and the ratchet compares debug builds
against optimized baselines. Both are declarative configuration, so
these tests parse `ci.yml` and assert the contract it must uphold.

The Makefile half of the same contract — the guard variable the recipe sets
and the module list it hands to pytest — lives in
`test_extension_build_contract.py`.

`yaml.safe_load` returns `typing.Any`, which erases every mistake an assertion
can make about the shape it reads: a misspelled key yields `None`, and the
assertion above it then passes or fails for a reason unrelated to the
contract. The shapes below declare the keys these tests reach for, so a typo
is a type error. Their *values* stay `object`, because they come from a file
this suite does not control and so are narrowed where they are read rather
than assumed at the boundary.
"""

from __future__ import annotations

import functools
import shlex
import typing as typ

import pytest
import yaml

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc

CI_WORKFLOW = ".github/workflows/ci.yml"


class Step(typ.TypedDict, total=False):
    """One step of a job, declaring only the keys these tests read."""

    run: object


class Job(typ.TypedDict, total=False):
    """One job of a workflow, declaring only the keys these tests read."""

    steps: list[Step]


class Workflow(typ.TypedDict, total=False):
    """A parsed workflow file, declaring only the keys these tests read."""

    jobs: dict[str, Job]


@functools.cache
def _workflow() -> Workflow:
    """Parse the CI workflow."""
    parsed = yaml.safe_load((repo_root() / CI_WORKFLOW).read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{CI_WORKFLOW} must parse to a mapping"
    return typ.cast("Workflow", parsed)


def _jobs() -> dict[str, Job]:
    """Return the workflow's jobs, keyed by name."""
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict), f"{CI_WORKFLOW} must declare a jobs mapping"
    return jobs


def _job_steps(job_name: str) -> list[Step]:
    """Return the steps of a named CI job."""
    jobs = _jobs()
    job = jobs.get(job_name)
    assert isinstance(job, dict), (
        f"{CI_WORKFLOW} must declare a {job_name!r} job; found {sorted(jobs)}"
    )
    steps = job.get("steps")
    assert isinstance(steps, list), f"the {job_name!r} job must declare steps"
    return steps


def _script_of(step: Step) -> str | None:
    """Return a step's ``run:`` script, or None when it runs no script."""
    script = step.get("run")
    return script if isinstance(script, str) else None


def _run_scripts() -> cabc.Iterator[tuple[str, str]]:
    """Yield the job name and script of every ``run:`` step in the workflow."""
    for job_name, job in _jobs().items():
        # A job that calls a reusable workflow declares `uses:` and no steps.
        for step in job.get("steps") or []:
            if (script := _script_of(step)) is not None:
                yield job_name, script


def _is_environment_assignment(token: str) -> bool:
    """Return whether *token* is a leading shell environment assignment."""
    if "=" not in token:
        return False
    name, _ = token.split("=", maxsplit=1)
    return name.isidentifier()


def _script_runs_command(script: str, command: str) -> bool:
    """Return whether *script* executes *command* as leading shell tokens."""
    expected = tuple(shlex.split(command))
    boundaries = frozenset({
        "&",
        "&&",
        ";",
        "|",
        "||",
        "if",
        "then",
        "elif",
        "else",
        "do",
    })

    for line in script.replace("\\\n", " ").splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
        segment_start = 0

        for index, token in enumerate([*tokens, ";"]):
            if token not in boundaries:
                continue
            segment = tokens[segment_start:index]
            while segment:
                if not _is_environment_assignment(segment[0]):
                    break
                segment.pop(0)
            if tuple(segment[: len(expected)]) == expected:
                return True
            segment_start = index + 1

    return False


def _first_step_running(command: str, *, job_name: str) -> tuple[int, str]:
    """Return the position and script of the first step running `command`."""
    for index, step in enumerate(_job_steps(job_name)):
        script = _script_of(step)
        if script is not None and _script_runs_command(script, command):
            return index, script
    pytest.fail(f"no step in the {job_name!r} job runs {command!r}")


def test_the_ci_job_builds_the_extension_before_running_the_gated_tests() -> None:
    """`extension-tests` must run `make develop` first.

    `make build` only syncs dependencies, so this ordering is the whole reason
    the job can pass at all, and nothing else asserts it.
    """
    build, _ = _first_step_running("make develop", job_name="extension-tests")
    tests, _ = _first_step_running("make test-extension", job_name="extension-tests")

    assert build < tests, (
        "the extension-tests job must build the extension with `make "
        "develop` before `make test-extension` runs; found the build at step "
        f"{build} and the tests at step {tests}"
    )


@pytest.mark.parametrize(
    ("script", "command", "expected"),
    [
        ("make develop", "make develop", True),
        ("make develop MATURIN_DEVELOP_FLAGS=--release", "make develop", True),
        ("TOOL=rust make develop", "make develop", True),
        ("9TOOL=rust make develop", "make develop", False),
        ("=rust make develop", "make develop", False),
        ("make " + "\\" + "\n" + "develop", "make develop", True),
        ("if make develop", "make develop", True),
        ("then make develop", "make develop", True),
        ("elif make develop", "make develop", True),
        ("else make develop", "make develop", True),
        ("do make develop", "make develop", True),
        ("# make develop", "make develop", False),
        ('echo "make develop"', "make develop", False),
        ("maturin develop", "maturin develop", True),
        ("# maturin develop", "maturin develop", False),
    ],
)
def test_script_runs_command_ignores_comments_and_non_commands(
    script: str,
    command: str,
    *,
    expected: bool,
) -> None:
    """The workflow matcher detects executable commands, not text mentions."""
    assert _script_runs_command(script, command) is expected, (
        f"expected script {script!r} to match command {command!r} as {expected}"
    )


def test_only_boundary_jobs_build_the_extension() -> None:
    """Only jobs isolated from the full suite may install the extension."""
    builders = {
        job_name
        for job_name, script in _run_scripts()
        if _script_runs_command(script, "make develop")
    }

    assert builders == {"benchmark-ratchet", "extension-tests"}, (
        "only the isolated extension and benchmark jobs may run `make develop`; "
        f"found {builders}"
    )


def test_the_benchmark_job_builds_through_the_develop_target() -> None:
    """`benchmark-ratchet` must reach an optimized build via `make develop`.

    Its numbers mean nothing against a debug build, so the flag matters
    as much as the shared target does.
    """
    _, script = _first_step_running("make develop", job_name="benchmark-ratchet")

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
        job_name
        for job_name, script in _run_scripts()
        if _script_runs_command(script, "maturin develop")
    })

    assert not offenders, (
        "these CI jobs invoke `maturin develop` directly instead of going "
        f"through `make develop`: {offenders}"
    )
