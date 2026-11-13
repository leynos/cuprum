"""Contract tests pinning the mutmut harness configuration in pyproject.toml.

mutmut copies only a fixed set of assets into its ``mutants/`` working tree;
anything the baseline suite reads from the repository root must be listed in
``[tool.mutmut].also_copy`` or the mutation baseline fails with a missing-file
or import error that no source-code mutant can explain. These tests read the
declared configuration back so a dropped entry fails here, on the pull request
that removed it, instead of in the mutation workflow.

mutmut skips a declared entry that does not exist without failing, so the
inventory is also checked against the filesystem. Directories must precede the
loose files they parent: mutmut creates parent directories only when copying a
directory.

The mutation run also excludes the two test modules that construct an
environment of their own (see ``_SANDBOX_EXCLUDED_MODULES``). The exclusion is
pinned here so it cannot widen silently, and each excluded module is checked to
still exist inside the selected suite, so the exclusion cannot hide a deletion.

Finally, the run loads a Hypothesis profile that relaxes one harness-specific
health check: mutmut reruns the suite in one process, so a class-based
``@given`` test is seen from several executors even though it is correct. The
profile is asserted to inherit the suite settings and suppress exactly that
check, so the relaxation cannot widen into the ordinary suite.

The root ``conftest.py`` must also import without importing ``cuprum``. mutmut
verifies its setup by arming the mutated package to raise from the first
trampolined call, and an exception escaping the run is that probe's expected
outcome. An import in the conftest body would instead be reported as a pytest
usage error, which mutmut refuses and aborts the whole run on.
"""

from __future__ import annotations

import dataclasses as dc
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell
import sys
import tomllib
from pathlib import PurePosixPath

import pytest
from hypothesis import HealthCheck, settings

from tests.helpers.docs import repo_root


@dc.dataclass(frozen=True, slots=True)
class _CopiedInput:
    """One repository-root input the mutation baseline requires."""

    path: str
    reader: str
    #: True for a directory entry, written with a trailing slash in TOML.
    is_directory: bool


#: The complete inventory of ``also_copy`` entries, in declaration order.
#: Each ``reader`` names the test or helper that fails without the input.
_COPIED_INPUTS: tuple[_CopiedInput, ...] = (
    _CopiedInput(
        "benchmarks/",
        "the benchmark and profiling tests import this package",
        is_directory=True,
    ),
    _CopiedInput(
        "docs/",
        "the documentation contract tests read these files",
        is_directory=True,
    ),
    _CopiedInput(
        ".github/",
        "the workflow and action contract tests read these files",
        is_directory=True,
    ),
    _CopiedInput(
        "scripts/",
        "tests/helpers and the release contracts import this package",
        is_directory=True,
    ),
    _CopiedInput(
        "tools/dev-fast/",
        "test_dev_fast_contract and test_dev_fast_adapter read the fragment",
        is_directory=True,
    ),
    _CopiedInput(
        "tools/mold/",
        "test_dev_fast_prerequisites and the Makefile read VERSION",
        is_directory=True,
    ),
    _CopiedInput(
        "rust/.config/",
        "the timeout-lane helpers read nextest.toml",
        is_directory=True,
    ),
    _CopiedInput(
        "rust/cuprum-rust/",
        "the wheel-build snapshot test builds from this crate",
        is_directory=True,
    ),
    _CopiedInput(
        "rust/cuprum-streams/",
        "the wheel-build snapshot test builds from this crate",
        is_directory=True,
    ),
    _CopiedInput(
        "rust/cuprum-native-io/",
        "the wheel-build snapshot test builds from this crate",
        is_directory=True,
    ),
    _CopiedInput(
        "conftest.py",
        "the shared root fixture module is loaded by pytest",
        is_directory=False,
    ),
    _CopiedInput(
        "README.md",
        "the documentation examples are read during collection",
        is_directory=False,
    ),
    _CopiedInput(
        ".yamllint.yml",
        "test_workflow_lint_provisioning reads the lint policy",
        is_directory=False,
    ),
    _CopiedInput(
        ".concordat",
        "the Rust build-footprint contracts read the build defaults",
        is_directory=False,
    ),
    _CopiedInput(
        "CHANGELOG.md",
        "test_changelog_records_cleanup_telemetry_contract reads the log",
        is_directory=False,
    ),
    _CopiedInput(
        "Makefile",
        "test_toolchain_pins reads the tool-version defaults",
        is_directory=False,
    ),
    _CopiedInput(
        "pyproject.toml",
        "the harness configuration and dependency-pin tests read it",
        is_directory=False,
    ),
    _CopiedInput(
        "rust/Cargo.toml",
        "the Rust build-footprint contracts read the workspace manifest",
        is_directory=False,
    ),
    _CopiedInput(
        "rust/Cargo.lock",
        "the Rust build-footprint contracts read the lock file",
        is_directory=False,
    ),
    _CopiedInput(
        "rust/clippy.toml",
        "the Rust lint contracts read the Clippy configuration",
        is_directory=False,
    ),
    _CopiedInput(
        "rust/dylint.toml",
        "the Rust lint contracts read the Dylint configuration",
        is_directory=False,
    ),
    _CopiedInput(
        "rust/rust-toolchain.toml",
        "the toolchain pins and build-footprint contracts read the toolchain",
        is_directory=False,
    ),
)

#: Path components that name build output or caches. Listing one would copy a
#: host-specific tree into the sandbox, so the harness must never name one.
_EXCLUDED_COMPONENTS: frozenset[str] = frozenset({
    "target",
    "build",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".git",
})

#: Parametrized so a failure names the input it concerns.
_COPIED_INPUT_CASES = tuple(
    pytest.param(copied, id=copied.path) for copied in _COPIED_INPUTS
)

#: Suite roots the mutation run collects.
_SELECTED_ROOTS: tuple[str, ...] = ("tests/", "cuprum/unittests/")

#: Test modules the mutation run must not collect.
#:
#: Each one constructs an environment inside the test: the nextest-route tests
#: drive ``uv run --frozen``, and the typing contract points ty at
#: ``sys.prefix``. mutmut rewrites every sandboxed module to import ``mutmut``
#: and installs itself only in an ephemeral ``uv run --with`` overlay, so
#: neither constructed environment can import the mutated ``cuprum``. The
#: assertions stay in the suite; only the mutation selection skips them.
_SANDBOX_EXCLUDED_MODULES: tuple[str, ...] = (
    "cuprum/unittests/test_nextest_route_floor.py",
    "cuprum/unittests/test_sh_typing_contract.py",
)

#: Hypothesis profile every mutmut pytest invocation loads.
#:
#: mutmut reruns the suite in a single process, so Hypothesis raises its
#: differing-executors health check for class-based property tests. The profile
#: is registered in the root ``conftest.py`` and must relax that one check only.
_SANDBOX_HYPOTHESIS_PROFILE: str = "mutmut-sandbox"


def _mutmut_table() -> dict[str, object]:
    """Return the ``[tool.mutmut]`` table declared in ``pyproject.toml``."""
    pyproject = tomllib.loads(
        (repo_root() / "pyproject.toml").read_text(encoding="utf-8")
    )
    match pyproject["tool"]["mutmut"]:
        case dict() as mutmut:
            return mutmut
        case _:
            pytest.fail("pyproject.toml must declare [tool.mutmut]")


def _string_list(table: dict[str, object], key: str) -> list[str]:
    """Return one string-list setting, failing when it is absent or mistyped."""
    match table.get(key):
        case list() as entries:
            return entries
        case _:
            pytest.fail(f"[tool.mutmut] must declare a {key} list")


def _also_copy_entries() -> list[str]:
    """Return the ``also_copy`` list declared under ``[tool.mutmut]``."""
    return _string_list(_mutmut_table(), "also_copy")


def _test_selection() -> list[str]:
    """Return the mutation run's pytest selection arguments."""
    return _string_list(_mutmut_table(), "pytest_add_cli_args_test_selection")


def _ignored_paths(selection: list[str]) -> set[str]:
    """Return the paths named by ``--ignore=`` entries in ``selection``."""
    return {
        entry.removeprefix("--ignore=")
        for entry in selection
        if entry.startswith("--ignore=")
    }


def test_also_copy_declares_exactly_the_inventoried_inputs() -> None:
    """The declared list matches the audited inventory, with no stray entries."""
    declared = set(_also_copy_entries())
    inventoried = {copied.path for copied in _COPIED_INPUTS}
    assert declared == inventoried, (
        "also_copy must name exactly the repository-root inputs the baseline "
        f"reads; missing: {sorted(inventoried - declared)}; "
        f"unexplained: {sorted(declared - inventoried)}"
    )


@pytest.mark.parametrize("copied", _COPIED_INPUT_CASES)
def test_every_declared_input_exists_with_its_declared_kind(
    copied: _CopiedInput,
) -> None:
    """A missing entry is skipped without failing, so the inventory is checked."""
    path = repo_root() / copied.path
    assert path.exists(), (
        f"{copied.reader}, but {copied.path} does not exist; mutmut would "
        "skip the entry without reporting it and the baseline would fail"
    )
    if copied.is_directory:
        assert path.is_dir(), (
            f"{copied.reader}, so {copied.path} must be a directory; "
            "the trailing slash in also_copy declares it as one"
        )
    else:
        assert path.is_file(), (
            f"{copied.reader}, so {copied.path} must be a file; a directory "
            "entry here would copy unrelated contents into the sandbox"
        )


def test_directories_precede_files_that_share_their_parent() -> None:
    """A loose file must follow a directory that creates its parent."""
    created = {PurePosixPath()}
    for entry in _also_copy_entries():
        path = PurePosixPath(entry)
        if entry.endswith("/"):
            created.add(path)
            created.update(path.parents)
            continue
        assert path.parent in created, (
            f"also_copy lists the loose file {entry!r} before any directory "
            f"that creates {path.parent}, so mutmut's copy step fails with "
            "FileNotFoundError"
        )


def test_also_copy_never_names_a_build_output_directory() -> None:
    """Build outputs and caches stay out of the sandbox."""
    offenders = [
        entry
        for entry in _also_copy_entries()
        if _EXCLUDED_COMPONENTS.intersection(PurePosixPath(entry).parts)
    ]
    assert not offenders, (
        f"also_copy must not copy build output or caches, but names {offenders}"
    )


def test_also_copy_does_not_copy_the_whole_rust_tree() -> None:
    """rust/ is copied piecewise so rust/target/ is never dragged in."""
    declared = {PurePosixPath(entry) for entry in _also_copy_entries()}
    assert PurePosixPath("rust") not in declared, (
        "also_copy must list the Rust crates and loose workspace files "
        "individually; copying rust/ wholesale includes rust/target/"
    )


def test_test_selection_still_collects_both_suite_roots() -> None:
    """Every suite root is collected, so an exclusion cannot shrink the run."""
    selection = _test_selection()
    missing = [root for root in _SELECTED_ROOTS if root not in selection]
    assert not missing, f"the mutation run must still collect {missing}"


def test_mutation_run_ignores_exactly_the_environment_constructing_modules() -> None:
    """The sandbox exclusions are the pinned modules and nothing else."""
    ignored = _ignored_paths(_test_selection())
    unexpected = sorted(ignored - set(_SANDBOX_EXCLUDED_MODULES))
    missing = sorted(set(_SANDBOX_EXCLUDED_MODULES) - ignored)
    assert not unexpected, (
        "the mutation run must not ignore modules beyond the ones that build "
        f"their own environment; unexpected: {unexpected}"
    )
    assert not missing, (
        "every module that builds its own environment must stay excluded; "
        f"missing: {missing}"
    )


def test_mutation_run_loads_the_sandbox_hypothesis_profile() -> None:
    """Every mutmut pytest invocation selects the sandbox profile."""
    args = _string_list(_mutmut_table(), "pytest_add_cli_args")
    assert args == [f"--hypothesis-profile={_SANDBOX_HYPOTHESIS_PROFILE}"], (
        "mutmut must load the sandbox Hypothesis profile on every run; "
        f"expected exactly the profile flag, got {args}"
    )


def test_sandbox_hypothesis_profile_relaxes_only_the_harness_check() -> None:
    """The profile inherits the suite settings and suppresses one check."""
    profile = settings.get_profile(_SANDBOX_HYPOTHESIS_PROFILE)
    assert profile.suppress_health_check == (HealthCheck.differing_executors,), (
        "the sandbox profile must suppress only the differing-executors "
        f"check, got {profile.suppress_health_check}"
    )
    assert profile.deadline is None, (
        "the sandbox profile must inherit the suite profile's unlimited "
        "deadline; registering it before the suite profile would silently "
        "restore Hypothesis's default deadline"
    )


def test_root_conftest_imports_without_the_package_under_test() -> None:
    """Importing the root conftest must not import ``cuprum``."""
    probe = (
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "import conftest\n"
        "print('cuprum' in sys.modules)\n"
    )
    # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv: the
    # running interpreter and a literal probe script, with no shell.
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"importing the root conftest must succeed, got exit "
        f"{result.returncode}: {result.stderr}"
    )
    assert result.stdout.strip() == "False", (
        "the root conftest must not import cuprum: mutmut's forced-fail probe "
        "runs with the mutated package armed to raise, and a conftest import "
        "failure is a pytest usage error that mutmut refuses; move the import "
        "into the fixture that needs it or declare its module in "
        "pytest_plugins"
    )


@pytest.mark.parametrize("module", _SANDBOX_EXCLUDED_MODULES)
def test_ignored_module_is_a_selected_test_module(module: str) -> None:
    """An ignored module still exists under a collected suite root."""
    path = repo_root() / module
    assert path.is_file(), (
        f"{module} is ignored by the mutation run but no longer exists; the "
        "exclusion must not outlive the module or hide its deletion"
    )
    is_under_selected_root = any(
        PurePosixPath(module).is_relative_to(root) for root in _SELECTED_ROOTS
    )
    assert is_under_selected_root, f"{module} is not under a collected suite root"
