"""Contract tests for the extension build wiring in the Makefile.

`make test-extension` is the only thing that turns a silently-skipped
extension into a failure, and it is configuration rather than code. Nothing
else in the suite notices when the guard variable leaves the recipe or a
module falls out of ``EXTENSION_TEST_TARGETS``: either change leaves a green
run behind. These tests read the wiring back and assert the contract it must
uphold. The CI half of the same contract — that a job builds the extension
before running against it — lives in `test_extension_ci_contract.py`.

The Makefile is read through ``make --dry-run`` rather than by parsing the
file: that is the command line the target actually runs, with every variable
expanded, so an assertion about the guard variable is an assertion about what
CI executes rather than about text sitting near it. That nested ``make`` runs
against a scrubbed environment, because otherwise it would report the caller's
configuration as though it were the repository's — see ``_caller_owned_names``.

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
import subprocess  # ruff: ignore[suspicious-subprocess-import] - reads the repository's own Makefile recipes.
import typing as typ

import pytest

from tests.helpers.docs import repo_root
from tests.helpers.extension_requirement import REQUIRE_EXTENSION_ENV

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


#: Matches a Makefile variable declared with ``?=``, which assigns only when
#: the name is not already defined — and a name present in the environment
#: counts as defined. ``make`` exports each of its own command-line overrides
#: under that name, so a suite run as ``make test EXTENSION_TEST_TARGETS=…``
#: would otherwise have the nested ``make`` below report the caller's list as
#: though the Makefile declared it. Stripping ``MAKEFLAGS`` does not prevent
#: that: the override travels under its own name as well.
_CONDITIONAL_ASSIGNMENT: typ.Final = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\?=", re.MULTILINE
)

#: ``make``'s own bookkeeping, which leaks in when pytest is itself launched
#: from ``make test``; an inherited jobserver flag makes the nested ``make``
#: warn about it.
_MAKE_BOOKKEEPING: typ.Final = frozenset({"MAKEFLAGS", "MAKELEVEL", "MFLAGS"})


@functools.cache
def _caller_owned_names() -> frozenset[str]:
    """Return environment names that would redefine a Makefile variable.

    Derived from the Makefile rather than listed here, so a variable gaining
    or losing its ``?=`` cannot leave the scrub quietly out of date.

    Returns
    -------
    frozenset[str]
        Names conditionally assigned in the Makefile, plus ``make``'s own
        bookkeeping variables.
    """
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    overridable = frozenset(_CONDITIONAL_ASSIGNMENT.findall(makefile))
    assert overridable, (
        "no `?=` assignments were found in the Makefile, so this scrub has "
        "stopped protecting anything; check the pattern still matches"
    )
    return overridable | _MAKE_BOOKKEEPING


def _make_dry_run(*goals: str) -> str:
    """Return what ``make --dry-run`` prints for `goals`, recipes expanded."""
    ignored = _caller_owned_names()
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argument vector.
        ["make", "--dry-run", *goals],  # ruff: ignore[start-process-with-partial-path] - `make` resolved from PATH.
        capture_output=True,
        check=True,
        cwd=repo_root(),
        env={key: value for key, value in os.environ.items() if key not in ignored},
        text=True,
    )
    return completed.stdout


@functools.cache
def _dry_run(*goals: str) -> str:
    """Cache `_make_dry_run`: nothing it reads changes during a session."""
    return _make_dry_run(*goals)


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

    Returns
    -------
    dict[str, str]
        Each scanned module's repo-relative path mapped to its source text.
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


def test_a_caller_variable_cannot_reach_the_nested_make(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inherited override must not become what these tests read back.

    Otherwise the contract holds or breaks according to how the suite was
    invoked: `make test EXTENSION_TEST_TARGETS=…` exports that name into
    pytest's environment, the Makefile's `?=` yields to it, and every
    assertion below reports on the caller instead of the repository.
    """
    intruder = "caller_owned_module.py"
    monkeypatch.setenv("EXTENSION_TEST_TARGETS", intruder)

    recipe = _make_dry_run("test-extension")

    assert intruder not in recipe, (
        "an inherited EXTENSION_TEST_TARGETS reached the nested make, so "
        "these tests would assert about whoever ran them rather than about "
        "the Makefile"
    )


def test_local_tool_environment_preserves_the_windows_action_path() -> None:
    """Windows must retain the PATH that setup-uv has already configured."""
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    match = re.search(
        r"^ifeq \(\$\(OS\),Windows_NT\)\n"
        r"(?P<windows>.*?)^else\n(?P<posix>.*?)^endif$",
        makefile,
        flags=re.MULTILINE | re.DOTALL,
    )

    assert match is not None, (
        "LOCAL_TOOL_ENV must branch explicitly for Windows_NT, so Git Bash "
        "keeps the setup-uv PATH instead of rebuilding it with POSIX separators"
    )

    windows = match["windows"]
    posix = match["posix"]
    assert re.fullmatch(r"LOCAL_TOOL_ENV\s*=\s*\n", windows) is not None, (
        "the Windows_NT branch must leave LOCAL_TOOL_ENV empty rather than "
        "prefixing or replacing PATH"
    )
    assert "PATH" not in windows, (
        "the Windows_NT branch must not rewrite PATH; setup-uv already adds "
        "the Windows uv directory"
    )
    assert "LOCAL_TOOL_PATH = $(HOME)/.local/bin:$(HOME)/.bun/bin:$(PATH)" in posix
    assert 'LOCAL_TOOL_ENV = PATH="$(LOCAL_TOOL_PATH)"' in posix


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


@pytest.mark.parametrize(
    ("goal_args", "expects_release_flag"),
    [
        pytest.param(("develop",), False, id="defaults_to_debug"),
        pytest.param(
            ("develop", "MATURIN_DEVELOP_FLAGS=--release"),
            True,
            id="forwards_release_flag",
        ),
    ],
)
def test_the_develop_target_release_flag(
    goal_args: tuple[str, ...], *, expects_release_flag: bool
) -> None:
    """`develop` stays a debug build unless `MATURIN_DEVELOP_FLAGS` says so.

    Builds run on every change stay unoptimized unless asked otherwise. The
    benchmark ratchet shares this target instead of restating the build, and
    it can only do that if the flag it passes actually takes effect.
    """
    command = _sole_line_containing(
        _dry_run(*goal_args), "maturin develop", subject="develop recipe"
    )

    assert ("--release" in command) is expects_release_flag, (
        f"expected --release presence to be {expects_release_flag} for "
        f"`make {' '.join(goal_args)}`"
    )
