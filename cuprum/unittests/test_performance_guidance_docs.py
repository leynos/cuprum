"""Documentation contract tests for roadmap item 4.5.2."""

from __future__ import annotations

from pathlib import Path

from tests.helpers import extract_markdown_subsection


def _repo_root() -> Path:
    """Return the repository root for documentation lookups."""
    return Path(__file__).resolve().parents[2]


def _read_doc(relative_path: str) -> str:
    """Return a documentation file as UTF-8 text."""
    return (_repo_root() / relative_path).read_text(encoding="utf-8")


def test_users_guide_includes_backend_choice_guidance() -> None:
    """Users' guide should tell readers how to choose a stream backend."""
    guide = _read_doc("docs/users-guide.md")

    section = extract_markdown_subsection(guide, heading="Choosing a stream backend")

    assert "`auto`" in section
    assert "`python`" in section
    assert "`rust`" in section
    assert "before first backend resolution in the process" in section
    assert "inter-stage pipeline pumping" in section
    assert "stdout/stderr capture" in section
    assert "Python pathway" in section
    assert "`make benchmark-e2e`" in section


def test_design_doc_matches_current_pumping_scope() -> None:
    """Design doc should match the current pumping-versus-capture scope."""
    design_doc = _read_doc("docs/cuprum-design.md")

    assert "Current Rust acceleration applies to inter-stage pipeline pumping" in (
        design_doc
    )
    assert "stdout/stderr capture" in design_doc
