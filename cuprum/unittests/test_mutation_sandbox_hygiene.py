"""Tests for the mutation sandbox's generated-directory purge.

mutmut 3.7.0 copies every ``[tool.mutmut].also_copy`` directory (and its
default ``tests/`` tree) with ``shutil.copytree`` and no ignore filter, so
whatever generated output the host worktree holds — ``__pycache__``,
``.pytest_cache``, ``.hypothesis``, a stray ``build/`` — arrives in
``mutants/`` with the sources. These tests pin the purge that removes those
directories from the statistics phase, and pin the guards that keep every
other process (including the sandbox's own subprocesses, which inherit
``MUTANT_UNDER_TEST``) out of it.
"""

from __future__ import annotations

import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell
import sys
import typing as typ

import pytest

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from pathlib import Path

from tests.helpers.sandbox_hygiene import (
    GENERATED_DIRECTORY_NAMES,
    is_mutation_sandbox_root,
    purge_generated_directories,
    purge_sandbox_generated_directories,
)


def _populate_generated_output(root: Path) -> dict[str, Path]:
    """Create one populated instance of every generated directory name."""
    created: dict[str, Path] = {}
    for name in sorted(GENERATED_DIRECTORY_NAMES):
        path = root / "scripts" / name / "payload"
        path.mkdir(parents=True)
        (path / "entry.txt").write_text("generated", encoding="utf-8")
        created[name] = path.parent
    return created


def test_purge_removes_populated_cache_directories(tmp_path: Path) -> None:
    """A populated cache directory copied with its parent is removed."""
    created = _populate_generated_output(tmp_path)
    (tmp_path / "scripts" / "release.py").write_text("", encoding="utf-8")

    removed = purge_generated_directories(tmp_path)

    assert set(removed) == {f"scripts/{name}" for name in GENERATED_DIRECTORY_NAMES}, (
        "the purge must report each removed directory once, root-relative"
    )
    for name, path in created.items():
        assert not path.exists(), f"{name} survived the purge at {path}"
    assert (tmp_path / "scripts" / "release.py").is_file(), (
        "the purge must leave the copied sources in place"
    )


def test_purge_keeps_lookalike_and_ordinary_directories(tmp_path: Path) -> None:
    """Only exact generated-directory names are removed."""
    lookalikes = {
        "docs/cache-notes": "notes.md",
        "docs/build-guide": "index.md",
        "scripts/targets": "release.py",
        "benchmarks/rebuild": "bench.py",
    }
    for directory, filename in lookalikes.items():
        path = tmp_path / directory
        path.mkdir(parents=True)
        (path / filename).write_text("", encoding="utf-8")

    assert purge_generated_directories(tmp_path) == ()

    for directory, filename in lookalikes.items():
        assert (tmp_path / directory / filename).is_file(), (
            f"{directory} only resembles a generated directory and must stay"
        )


def test_purge_is_idempotent(tmp_path: Path) -> None:
    """A second purge finds nothing left to remove."""
    _populate_generated_output(tmp_path)

    assert purge_generated_directories(tmp_path)
    assert purge_generated_directories(tmp_path) == ()


def test_purge_never_removes_the_root_itself(tmp_path: Path) -> None:
    """A root whose own name is generated output is kept."""
    root = tmp_path / "__pycache__"
    root.mkdir()
    (root / "kept.txt").write_text("", encoding="utf-8")

    assert purge_generated_directories(root) == ()
    assert (root / "kept.txt").is_file()


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("stats", True),
        ("list_all_tests", True),
        ("fail", False),
        ("", False),
        (None, False),
    ],
)
def test_sandbox_root_requires_a_reading_phase(
    tmp_path: Path, phase: str | None, *, expected: bool
) -> None:
    """Only the phases that read the sandbox are eligible to purge."""
    (tmp_path / ".git").mkdir()
    sandbox = tmp_path / "mutants"
    sandbox.mkdir()
    environ = {} if phase is None else {"MUTANT_UNDER_TEST": phase}

    assert is_mutation_sandbox_root(sandbox, environ) is expected


def test_sandbox_root_requires_mutmut_directory_name(tmp_path: Path) -> None:
    """A temporary directory that inherits the phase selection is skipped."""
    (tmp_path / ".git").mkdir()
    elsewhere = tmp_path / "work"
    elsewhere.mkdir()

    assert not is_mutation_sandbox_root(elsewhere, {"MUTANT_UNDER_TEST": "stats"})


def test_sandbox_root_accepts_a_worktree_git_file(tmp_path: Path) -> None:
    """A git worktree records ``.git`` as a file, not a directory."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    sandbox = tmp_path / "mutants"
    sandbox.mkdir()

    assert is_mutation_sandbox_root(sandbox, {"MUTANT_UNDER_TEST": "stats"})


def test_sandbox_purge_skips_a_subprocess_temporary_directory(
    tmp_path: Path,
) -> None:
    """A fixture's own working tree is untouched despite the inherited env."""
    work = tmp_path / "fixture-work"
    (work / "build").mkdir(parents=True)
    (work / "build" / "artefact.txt").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()

    removed = purge_sandbox_generated_directories(work, {"MUTANT_UNDER_TEST": "stats"})

    assert removed == ()
    assert (work / "build" / "artefact.txt").is_file()


#: Script the probe runs in a fresh interpreter: import the root conftest with
#: the sandbox as its working directory, then report what it observed.
_CONFTEST_PROBE = (
    "import importlib.util\n"
    "import os\n"
    "import sys\n"
    "import types\n"
    "repo = sys.argv[1]\n"
    "cache = sys.argv[2]\n"
    "sys.path.insert(0, repo)\n"
    "fake = types.ModuleType('cuprum')\n"
    "backend = types.ModuleType('cuprum._rust_backend')\n"
    "backend.is_available = lambda: False\n"
    "fake._rust_backend = backend\n"
    "sys.modules['cuprum'] = fake\n"
    "sys.modules['cuprum._rust_backend'] = backend\n"
    "spec = importlib.util.spec_from_file_location(\n"
    "    'root_conftest', repo + '/conftest.py'\n"
    ")\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "print('gone_after_import', not os.path.exists(cache))\n"
    "present = module._PROJECT_IMPORTS_PRESENT_AT_BOOTSTRAP\n"
    "print('project_imports_present', present)\n"
    "module.pytest_configure(None)\n"
)


def _sandbox_probe_environment() -> dict[str, str]:
    """Return the probe's environment: the statistics phase, extension optional."""
    return {
        **os.environ,
        "MUTANT_UNDER_TEST": "stats",
        "CUPRUM_REQUIRE_RUST_EXTENSION": "",
    }


def test_purge_runs_before_collection_on_a_resumed_run(tmp_path: Path) -> None:
    """A resumed run purges the cache it copied before listing tests."""
    (tmp_path / ".git").mkdir()
    sandbox = tmp_path / "mutants"
    cache = sandbox / "scripts" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "release.cpython-313.pyc").write_bytes(b"")

    removed = purge_sandbox_generated_directories(
        sandbox, {"MUTANT_UNDER_TEST": "list_all_tests"}
    )

    assert "scripts/__pycache__" in removed, (
        "the collection pass of a resumed run copies also_copy again, so the "
        f"purge must run there too; removed: {removed}"
    )
    assert not cache.exists(), "the copied bytecode must not survive to collection"


def test_pytest_configure_purges_the_statistics_sandbox(tmp_path: Path) -> None:
    """The root conftest runs the purge from ``pytest_configure``."""
    (tmp_path / ".git").mkdir()
    sandbox = tmp_path / "mutants"
    cache = sandbox / "scripts" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "release.cpython-313.pyc").write_bytes(b"")
    (sandbox / "scripts" / "release.py").write_text("", encoding="utf-8")
    # The bootstrap removes __pycache__ itself; this directory survives it, so
    # the later pytest_configure purge still has something to report.
    (sandbox / ".pytest_cache").mkdir()
    (sandbox / ".pytest_cache" / "CACHEDIR.TAG").write_text("", encoding="utf-8")

    # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv: the
    # running interpreter and a literal probe script, with no shell.
    result = subprocess.run(
        [sys.executable, "-I", "-c", _CONFTEST_PROBE, str(repo_root()), str(cache)],
        cwd=sandbox,
        env=_sandbox_probe_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"the root conftest must run pytest_configure, got exit "
        f"{result.returncode}: {result.stderr}"
    )
    assert "project_imports_present False" in result.stdout, (
        "the tests.helpers package must not be imported before the bootstrap "
        "runs: its __init__ imports helpers eagerly, so a later purge would "
        "leave modules already loaded from copied bytecode"
    )
    assert "gone_after_import True" in result.stdout, (
        "the copied cache must already be gone when the root conftest finishes "
        "importing: the purge has to run before any project module is loaded, "
        "or the sandbox can execute the host's bytecode"
    )
    assert not cache.exists(), (
        "pytest_configure must purge the sandbox's copied caches: mutmut's "
        "copytree has no ignore filter, so the host's bytecode otherwise "
        "reaches the baseline run"
    )
    assert (sandbox / "scripts" / "release.py").is_file(), (
        "the purge must not remove the copied sources"
    )
    assert "purged 1 generated directories" in result.stdout
