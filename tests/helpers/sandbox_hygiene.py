"""Purge host-generated output copied into the mutation sandbox.

mutmut 3.7.0 builds ``mutants/`` by copying ``source_paths``, the default
``tests/`` tree, and every ``[tool.mutmut].also_copy`` entry with
``shutil.copytree`` and no ignore filter. A ``__pycache__``,
``.pytest_cache``, or ``.hypothesis`` directory left in the host worktree by
an earlier run is therefore copied alongside the sources, and the sandbox then
executes tests against bytecode and cache state compiled by a different
environment.

mutmut runs its statistics phase first, once, in the process that owns the
sandbox, and only then forks the mutant workers. Purging the generated
directories there removes the host's leftovers before any test observes them,
while the caches that the statistics phase itself creates stay in place for the
remaining phases.

The decision and the purge live here rather than in the root ``conftest.py``
so they can be tested directly: the root conftest is shadowed by the
per-package one and cannot be imported by name from a test module.
"""

from __future__ import annotations

import os
import shutil
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: Environment variable mutmut uses to select the phase of the run.
MUTATION_PHASE_ENV: typ.Final[str] = "MUTANT_UNDER_TEST"

#: The value mutmut sets while collecting the baseline statistics.
STATS_PHASE: typ.Final[str] = "stats"

#: Name mutmut gives the sandbox directory it builds beside the sources.
SANDBOX_DIRECTORY_NAME: typ.Final[str] = "mutants"

#: Directory names that hold generated output or interpreter caches.
#:
#: They are matched at any depth: ``scripts/__pycache__`` arrives with the
#: ``scripts/`` copy, ``tests/.pytest_cache`` with mutmut's default ``tests/``
#: copy, and ``rust/target`` only when someone copies ``rust/`` whole.
GENERATED_DIRECTORY_NAMES: typ.Final[frozenset[str]] = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    ".cache",
    ".uv-cache",
    ".pypy",
    "build",
    "dist",
    "node_modules",
    "target",
})


def is_mutation_sandbox_root(root: Path, environ: cabc.Mapping[str, str]) -> bool:
    """Report whether ``root`` is mutmut's sandbox during the stats phase.

    Parameters
    ----------
    root : pathlib.Path
        Candidate sandbox directory, normally ``Path.cwd()``.
    environ : cabc.Mapping[str, str]
        Process environment; normally ``os.environ``.

    Returns
    -------
    bool
        ``True`` when the process is mutmut's statistics phase and ``root``
        looks like the sandbox it built.

    Notes
    -----
    ``MUTANT_UNDER_TEST`` is inherited by every subprocess the suite starts,
    so the variable alone cannot decide. A fixture that runs ``pytest`` in a
    temporary directory would otherwise have that directory's ``build`` or
    ``target`` purged mid-test. The sandbox is identified by all three of its
    stable properties: mutmut's directory name, a parent that is a git
    worktree, and the statistics phase selection.

    Examples
    --------
    An ordinary test run, and every subprocess it starts, is never the
    statistics phase:

    >>> is_mutation_sandbox_root(Path("/repo/mutants"), {})
    False
    """
    return (
        environ.get(MUTATION_PHASE_ENV) == STATS_PHASE
        and root.name == SANDBOX_DIRECTORY_NAME
        and (root.parent / ".git").exists()
    )


def purge_generated_directories(root: Path) -> tuple[str, ...]:
    """Delete generated directories beneath ``root``.

    Parameters
    ----------
    root : pathlib.Path
        Tree to prune. The root itself is never removed, even when its own
        name matches :data:`GENERATED_DIRECTORY_NAMES`.

    Returns
    -------
    tuple[str, ...]
        Root-relative POSIX paths of the removed directories, in walk order.
        Empty when nothing matched.

    Examples
    --------
    A tree with no generated directories is left untouched:

    >>> purge_generated_directories(Path("/nonexistent"))
    ()
    """
    removed: list[str] = []
    for parent, dirnames, _filenames in os.walk(root):
        kept: list[str] = []
        for name in dirnames:
            path = Path(parent) / name
            if name not in GENERATED_DIRECTORY_NAMES:
                kept.append(name)
                continue
            # mutmut copies with symlinks=False, so a sandbox entry is a real
            # directory; unlink any link defensively rather than letting
            # shutil.rmtree refuse it.
            if path.is_symlink():
                path.unlink()
            else:
                shutil.rmtree(path)
            removed.append(path.relative_to(root).as_posix())
        dirnames[:] = sorted(kept)
    return tuple(removed)


def purge_sandbox_generated_directories(
    root: Path, environ: cabc.Mapping[str, str]
) -> tuple[str, ...]:
    """Purge generated directories when ``root`` is the mutation sandbox.

    Parameters
    ----------
    root : pathlib.Path
        Candidate sandbox directory, normally ``Path.cwd()``.
    environ : cabc.Mapping[str, str]
        Process environment; normally ``os.environ``.

    Returns
    -------
    tuple[str, ...]
        Root-relative POSIX paths of the removed directories, or an empty
        tuple when this process is not the sandbox's statistics phase.

    Examples
    --------
    Outside the statistics phase nothing is removed:

    >>> purge_sandbox_generated_directories(Path.cwd(), {})
    ()
    """
    if not is_mutation_sandbox_root(root, environ):
        return ()
    return purge_generated_directories(root)
