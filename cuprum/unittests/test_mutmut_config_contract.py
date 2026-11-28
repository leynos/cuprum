"""Contract tests pinning the mutmut copied-input inventory.

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

The selection and preflight faces of the same harness live in
``test_mutmut_selection_contract.py`` and
``test_mutation_preflight_contract.py``; the inventory they share is in
``_mutation_config_support.py``.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from cuprum.unittests._mutation_config_support import (
    COPIED_INPUTS,
    CopiedInput,
    mutmut_table,
    string_list,
)
from tests.helpers.docs import repo_root

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
    pytest.param(copied, id=copied.path) for copied in COPIED_INPUTS
)


def _also_copy_entries() -> list[str]:
    """Return the ``also_copy`` list declared under ``[tool.mutmut]``."""
    return string_list(mutmut_table(), "also_copy")


def test_also_copy_declares_exactly_the_inventoried_inputs() -> None:
    """The declared list matches the audited inventory, with no stray entries."""
    declared = set(_also_copy_entries())
    inventoried = {copied.path for copied in COPIED_INPUTS}
    assert declared == inventoried, (
        "also_copy must name exactly the repository-root inputs the baseline "
        f"reads; missing: {sorted(inventoried - declared)}; "
        f"unexplained: {sorted(declared - inventoried)}"
    )


@pytest.mark.parametrize("copied", _COPIED_INPUT_CASES)
def test_every_declared_input_exists_with_its_declared_kind(
    copied: CopiedInput,
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
