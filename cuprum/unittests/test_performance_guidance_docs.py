"""Documentation contract tests for roadmap item 4.5.2."""

from __future__ import annotations

from pathlib import Path

from tests.helpers import extract_markdown_subsection
from tests.helpers.docs import assert_documents


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

    for term in (
        "`auto`",
        "`python`",
        "`rust`",
        "before first backend resolution in the process",
        "inter-stage pipeline pumping",
        "stdout/stderr capture",
        "Python pathway",
        "`make benchmark-e2e`",
    ):
        assert_documents(section, term)


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
