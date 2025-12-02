"""Unit tests for cuprum public exports."""

from __future__ import annotations

import cuprum as c


def test_public_exports_are_available() -> None:
    """Top-level cuprum exports default catalogue symbols."""
    assert c.DEFAULT_CATALOGUE is not None
    assert c.DEFAULT_PROJECTS
    assert c.CORE_OPS_PROJECT == "core-ops"
    assert c.DOCUMENTATION_PROJECT == "docs"
    assert c.Program("echo") == c.ECHO
    assert c.Program("ls") == c.LS
    assert c.Program("mdbook") == c.DOC_TOOL
    assert c.ProgramCatalogue is not None
    assert c.ProgramEntry is not None
    assert c.ProjectSettings is not None
    assert c.UnknownProgramError is not None


def test_public_catalogue_behaviour_via_reexports() -> None:
    """Catalogue lookups work through the re-exported API surface."""
    entry = c.DEFAULT_CATALOGUE.lookup(c.ECHO)
    assert entry.program == c.Program("echo")
    assert entry.project_name == c.CORE_OPS_PROJECT
    assert c.DEFAULT_CATALOGUE.is_allowed("ls")
    assert not c.DEFAULT_CATALOGUE.is_allowed("definitely-not-allowed")
