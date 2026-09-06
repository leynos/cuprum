"""Contract tests pinning the mutmut harness configuration in pyproject.toml.

mutmut copies only a fixed set of assets into its ``mutants/`` working tree;
anything the suite reads from the repository root must be listed in
``[tool.mutmut].also_copy`` or the mutation baseline fails with a missing-file
error that no source-code mutant can explain. These tests read the declared
configuration back so a dropped entry fails here, on the pull request that
removed it, instead of in the mutation workflow.
"""

from __future__ import annotations

import tomllib

import pytest

from tests.helpers.docs import repo_root

#: Loose root-level files that mutmut must copy into its mutants/ tree. Each
#: entry is read from the repository root by the test named in the comment;
#: dropping one breaks the mutation baseline rather than the local suite.
_LOOSE_FILES_IN_ALSO_COPY: tuple[tuple[str, str], ...] = (
    ("CHANGELOG.md", "test_changelog_records_cleanup_telemetry_contract"),
    ("Makefile", "test_toolchain_pins"),
)


def _also_copy_entries() -> list[str]:
    """Return the ``also_copy`` list declared under ``[tool.mutmut]``."""
    pyproject = tomllib.loads(
        (repo_root() / "pyproject.toml").read_text(encoding="utf-8")
    )
    match pyproject["tool"]["mutmut"]:
        case dict() as mutmut:
            entries = mutmut.get("also_copy")
        case _:
            pytest.fail("pyproject.toml must declare [tool.mutmut]")
    match entries:
        case list() as entries:
            return entries
        case _:
            pytest.fail("[tool.mutmut] must declare an also_copy list")


def test_also_copy_copies_loose_files_read_from_the_repository_root() -> None:
    """Every loose root file a test reads must be listed in ``also_copy``."""
    also_copy = _also_copy_entries()
    for filename, reader in _LOOSE_FILES_IN_ALSO_COPY:
        assert filename in also_copy, (
            f"{reader} reads the loose root-level {filename}, so [tool.mutmut]"
            f".also_copy must include {filename!r} or the mutation baseline "
            "fails with a missing-file error in mutmut's mutant tree"
        )
