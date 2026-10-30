"""Live-Git regressions for ``git_checkout``'s revision-only contract.

The builder emits ``git checkout <ref> --`` in switch modes. Without that
trailing separator, a value that matches a tracked path but no branch is read
by Git as a path restore, silently discarding uncommitted edits (issue #546).
These tests run the installed Git so the ambiguity is demonstrated, not merely
asserted at argv level.

Every test skips when Git is missing. Each test builds its own repository under
``tmp_path`` with an isolated configuration and identity, so the host's global
config, hooks, and default branch cannot affect the result.
"""

from __future__ import annotations

import dataclasses as dc
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs the installed Git.
import typing as typ

import pytest

from cuprum import ExecutionContext, ScopeConfig, scoped
from cuprum.builders import git_checkout
from cuprum.catalogue import GIT
from cuprum.context import EnvMode

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import CommandResult

_GIT_AVAILABLE = shutil.which("git") is not None
_GIT_SKIP_REASON = "these tests exercise the git on PATH, which this host lacks"
_GIT_TIMEOUT = 20.0
_PYTEST_TIMEOUT = 25.0

pytestmark = pytest.mark.skipif(not _GIT_AVAILABLE, reason=_GIT_SKIP_REASON)

_AUTHOR = "Cuprum Test"
_EMAIL = "cuprum@example.invalid"


@dc.dataclass(frozen=True, slots=True)
class _Repository:
    """A throwaway repository and the environment that isolates its Git."""

    root: Path
    env: dict[str, str]


@dc.dataclass(frozen=True, slots=True)
class _Snapshot:
    """Observable state a checkout must either change or leave alone."""

    head: str
    branch: str
    index: str
    status: str


@pytest.fixture(scope="session")
def git_version() -> str:
    """Skip unless the installed Git runs and supports the setup used here."""
    completed = subprocess.run(
        ["git", "--version"],  # ruff: ignore[start-process-with-partial-path] - same executable the builder runs.
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT,
    )
    if completed.returncode != 0:
        pytest.skip(f"`git --version` failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _isolated_environment(home: Path) -> dict[str, str]:
    """Return a child environment with no inherited Git configuration."""
    # The ambient environment may carry GIT_CONFIG_COUNT/KEY/VALUE entries or
    # a Lody remote rewrite. Dropping every GIT_ variable and replacing the
    # environment keeps the probes independent of the host while retaining
    # PATH and the rest of the platform environment.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": _AUTHOR,
            "GIT_AUTHOR_EMAIL": _EMAIL,
            "GIT_COMMITTER_NAME": _AUTHOR,
            "GIT_COMMITTER_EMAIL": _EMAIL,
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": os.environ.get("PATH", ""),
        },
    )
    return env


def _run_git(repo: _Repository, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one Git command in the repository without asserting its status."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - test-controlled argv.
        ["git", *args],  # ruff: ignore[start-process-with-partial-path] - the installed Git.
        cwd=repo.root,
        env=repo.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT,
    )


def _git_ok(repo: _Repository, *args: str) -> str:
    """Run one Git command and assert success, returning its stdout."""
    completed = _run_git(repo, *args)
    assert completed.returncode == 0, (
        f"git {' '.join(args)} failed with {completed.returncode}: {completed.stderr}"
    )
    return completed.stdout


def _commit(repo: _Repository, files: dict[str, str], message: str) -> str:
    """Write ``files``, commit them, and return the new commit ID."""
    for name, content in files.items():
        path = repo.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git_ok(repo, "add", "--", *files)
    _git_ok(repo, "commit", "-m", message)
    return _git_ok(repo, "rev-parse", "HEAD").strip()


def _current_branch(repo: _Repository) -> str:
    """Return the checked-out branch, or an empty string when detached."""
    return _run_git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()


def _snapshot(repo: _Repository) -> _Snapshot:
    """Capture HEAD, branch, index and worktree status."""
    return _Snapshot(
        head=_git_ok(repo, "rev-parse", "HEAD").strip(),
        branch=_current_branch(repo),
        index=_git_ok(repo, "ls-files", "--stage"),
        status=_git_ok(repo, "status", "--porcelain=v1"),
    )


def _run_checkout(repo: _Repository, ref: str, **kwargs: bool) -> CommandResult:
    """Execute the real builder inside a scope that allows Git."""
    command = git_checkout(ref, **kwargs)
    with scoped(ScopeConfig(allowlist=frozenset({GIT}))):
        return command.run_sync(
            context=ExecutionContext(
                cwd=repo.root,
                env=repo.env,
                env_mode=EnvMode.REPLACE,
            ),
        )


@pytest.fixture
def repo(tmp_path: Path, git_version: str) -> _Repository:
    """Create an isolated repository whose initial branch is ``main``."""
    del git_version
    root = tmp_path / "repo"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    repository = _Repository(root=root, env=_isolated_environment(home))
    _git_ok(repository, "init", "--initial-branch=main")
    return repository


@pytest.mark.parametrize("path", ["notes.txt", "docs/notes.txt"])
@pytest.mark.parametrize(
    "kwargs",
    [{}, {"force": True}, {"detach": True}],
    ids=["default", "force", "detach"],
)
@pytest.mark.timeout(_PYTEST_TIMEOUT)
def test_path_only_operand_cannot_restore_a_tracked_file(
    repo: _Repository,
    path: str,
    kwargs: dict[str, bool],
) -> None:
    """A path-like ref is refused and the uncommitted edit survives.

    This reproduces the reported finding. Unpatched, the default and force
    rows fail because ``git checkout notes.txt`` succeeds and restores the
    file from the index; the detach row already passes, because ``--detach``
    is itself unambiguous and forces revision resolution. No branch or tag
    shares either name, so only the pathspec interpretation can accept the
    value.
    """
    _commit(repo, {path: "committed\n"}, "add the tracked file")
    edited = f"distinctive uncommitted bytes for {path}\n"
    tracked = repo.root / path
    tracked.write_text(edited, encoding="utf-8")
    before = _snapshot(repo)
    assert tracked.read_text(encoding="utf-8") == edited
    assert path in before.status, "the edit must start out modified"

    result = _run_checkout(repo, path, **kwargs)

    assert not result.ok, (
        f"git checkout {path} must refuse a path-only operand "
        f"(exit {result.exit_code}: {result.stderr})"
    )
    assert tracked.read_text(encoding="utf-8") == edited, (
        "a refused checkout must not restore the tracked file"
    )
    assert _snapshot(repo) == before, (
        "a refused checkout must not change HEAD, branch, index or status"
    )


@pytest.mark.timeout(_PYTEST_TIMEOUT)
def test_branch_switch_preserves_an_unrelated_dirty_file(
    repo: _Repository,
) -> None:
    """A legitimate branch switch still works and keeps unrelated edits."""
    _commit(repo, {"common.txt": "common\n", "dirty.txt": "base\n"}, "base")
    _git_ok(repo, "checkout", "-b", "feature/one")
    feature_head = _commit(
        repo,
        {"feature.txt": "feature only\n"},
        "feature commit",
    )
    _git_ok(repo, "checkout", "main")

    edited = "unrelated dirty bytes\n"
    (repo.root / "dirty.txt").write_text(edited, encoding="utf-8")

    result = _run_checkout(repo, "feature/one")

    assert result.ok, f"branch switch failed: {result.stderr}"
    after = _snapshot(repo)
    assert after.branch == "feature/one", "the requested branch must be current"
    assert after.head == feature_head, "HEAD must be the branch's commit"
    assert (repo.root / "feature.txt").read_text(encoding="utf-8") == (
        "feature only\n"
    ), "the branch-specific file must be present"
    assert (repo.root / "dirty.txt").read_text(encoding="utf-8") == edited, (
        "an unrelated dirty file must survive the switch byte-for-byte"
    )


@pytest.mark.timeout(_PYTEST_TIMEOUT)
def test_conflicting_branch_switch_is_refused_without_loss(
    repo: _Repository,
) -> None:
    """A non-force switch that would overwrite edits fails and changes nothing."""
    _commit(repo, {"notes.txt": "main\n"}, "main commit")
    _git_ok(repo, "checkout", "-b", "feature/changing")
    _commit(repo, {"notes.txt": "branch\n"}, "changing commit")
    _git_ok(repo, "checkout", "main")

    edited = "main with local edits\n"
    tracked = repo.root / "notes.txt"
    tracked.write_text(edited, encoding="utf-8")
    before = _snapshot(repo)

    result = _run_checkout(repo, "feature/changing")

    assert not result.ok, "a conflicting switch without force must fail"
    assert tracked.read_text(encoding="utf-8") == edited, "the edit must survive"
    assert _snapshot(repo) == before, (
        "a refused switch must not change repository state"
    )


@pytest.mark.parametrize("kind", ["tag", "commit"])
@pytest.mark.timeout(_PYTEST_TIMEOUT)
def test_clean_repository_checkout_resolves_tag_and_commit(
    repo: _Repository,
    kind: str,
) -> None:
    """Tags and full commit IDs remain checkable and detach HEAD."""
    first = _commit(repo, {"notes.txt": "one\n"}, "first")
    second = _commit(repo, {"notes.txt": "two\n"}, "second")
    _git_ok(repo, "tag", "v1", first)
    revision, expected_head, expected_text = {
        "tag": ("v1", first, "one\n"),
        "commit": (second, second, "two\n"),
    }[kind]

    result = _run_checkout(repo, revision)

    assert result.ok, f"checkout {revision} failed: {result.stderr}"
    after = _snapshot(repo)
    assert after.head == expected_head, "HEAD must point at the selected commit"
    assert not after.branch, "a tag or commit checkout must detach HEAD"
    assert (repo.root / "notes.txt").read_text(encoding="utf-8") == expected_text


@pytest.mark.timeout(_PYTEST_TIMEOUT)
def test_detach_mode_selects_the_named_branch_commit(repo: _Repository) -> None:
    """``detach=True`` leaves HEAD at the branch's commit without a branch."""
    main_head = _commit(repo, {"notes.txt": "main\n"}, "main commit")
    _git_ok(repo, "checkout", "-b", "feature/other")
    _commit(repo, {"notes.txt": "other\n"}, "other commit")

    result = _run_checkout(repo, "main", detach=True)

    assert result.ok, f"detached checkout failed: {result.stderr}"
    after = _snapshot(repo)
    assert after.head == main_head, "detached HEAD must be the main commit"
    assert not after.branch, "detach=True must not leave a branch checked out"
