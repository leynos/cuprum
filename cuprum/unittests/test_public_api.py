"""Unit tests for cuprum public exports."""

from __future__ import annotations

import cuprum as c


def test_public_exports_are_available() -> None:
    """Top-level cuprum exports default catalogue symbols."""
    assert c.DEFAULT_CATALOGUE is not None, "DEFAULT_CATALOGUE must be exported"
    assert c.DEFAULT_PROJECTS, "DEFAULT_PROJECTS must not be empty"
    assert c.CORE_OPS_PROJECT == "core-ops", "CORE_OPS_PROJECT value mismatch"
    assert c.DOCUMENTATION_PROJECT == "docs", "DOCUMENTATION_PROJECT value mismatch"
    assert c.Program("echo") == c.ECHO, "ECHO must round-trip via Program"
    assert c.Program("ls") == c.LS, "LS must round-trip via Program"
    assert c.Program("mdbook") == c.DOC_TOOL, "DOC_TOOL must round-trip via Program"
    assert c.ProgramCatalogue is not None, "ProgramCatalogue must be exported"
    assert c.ProgramEntry is not None, "ProgramEntry must be exported"
    assert c.ProjectSettings is not None, "ProjectSettings must be exported"
    assert c.UnknownProgramError is not None, "UnknownProgramError must be exported"
    assert c.Pipeline is not None, "Pipeline must be exported"
    assert c.PipelineResult is not None, "PipelineResult must be exported"


def test_public_catalogue_behaviour_via_reexports() -> None:
    """Catalogue lookups work through the re-exported API surface."""
    entry = c.DEFAULT_CATALOGUE.lookup(c.ECHO)
    assert entry.program == c.Program("echo"), "Lookup must return typed Program"
    assert entry.project_name == c.CORE_OPS_PROJECT, "Project name mismatch"
    assert c.DEFAULT_CATALOGUE.is_allowed("ls"), "Curated program ls must be allowed"
    assert not c.DEFAULT_CATALOGUE.is_allowed("definitely-not-allowed"), (
        "Unknown program should not be allowlisted"
    )
