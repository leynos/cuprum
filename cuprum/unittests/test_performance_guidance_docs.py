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

    assert "`auto`" in section, "Missing documentation clause: '`auto`'"
    assert "`python`" in section, "Missing documentation clause: '`python`'"
    assert "`rust`" in section, "Missing documentation clause: '`rust`'"
    assert "before first backend resolution in the process" in section, (
        "Missing documentation clause: 'before first backend resolution in the process'"
    )
    assert "inter-stage pipeline pumping" in section, (
        "Missing documentation clause: 'inter-stage pipeline pumping'"
    )
    assert "stdout/stderr capture" in section, (
        "Missing documentation clause: 'stdout/stderr capture'"
    )
    assert "Python pathway" in section, "Missing documentation clause: 'Python pathway'"
    assert "`make benchmark-e2e`" in section, (
        "Missing documentation clause: '`make benchmark-e2e`'"
    )


def test_design_doc_matches_current_pumping_scope() -> None:
    """Design doc should match the current pumping-versus-capture scope."""
    design_doc = _read_doc("docs/cuprum-design.md")

    assert "Current Rust acceleration applies to inter-stage pipeline pumping" in (
        design_doc
    ), (
        "Missing design-doc clause: "
        "'Current Rust acceleration applies to inter-stage pipeline pumping'"
    )
    assert "stdout/stderr capture" in design_doc, (
        "Missing design-doc clause: 'stdout/stderr capture'"
    )
