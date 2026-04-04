"""Documentation contract tests for roadmap items 4.5.3 and 4.5.4."""

from __future__ import annotations

from pathlib import Path

from tests.helpers import extract_markdown_subsection


def _repo_root() -> Path:
    """Return the repository root for documentation lookups."""
    return Path(__file__).resolve().parents[2]


def _read_doc(relative_path: str) -> str:
    """Return a documentation file as UTF-8 text."""
    return (_repo_root() / relative_path).read_text(encoding="utf-8")


# -- 4.5.3: Build prerequisites -----------------------------------------------


def test_users_guide_has_build_prerequisites_section() -> None:
    """Users' guide should contain a build prerequisites heading."""
    guide = _read_doc("docs/users-guide.md")

    section = extract_markdown_subsection(
        guide, heading="Build prerequisites for native extensions"
    )

    assert "Rust" in section, "Missing documentation clause: 'Rust'"
    assert "1.85" in section, "Missing documentation clause: '1.85'"
    assert "maturin" in section, "Missing documentation clause: 'maturin'"
    assert "cargo" in section, "Missing documentation clause: 'cargo'"
    assert "`maturin develop`" in section, (
        "Missing documentation clause: '`maturin develop`'"
    )


def test_users_guide_build_prerequisites_mention_rustup() -> None:
    """Users' guide should mention rustup as the installation method."""
    guide = _read_doc("docs/users-guide.md")

    section = extract_markdown_subsection(
        guide, heading="Build prerequisites for native extensions"
    )

    assert "rustup" in section, "Missing documentation clause: 'rustup'"


def test_users_guide_build_prerequisites_mention_verification() -> None:
    """Users' guide should tell readers how to verify the Rust extension."""
    guide = _read_doc("docs/users-guide.md")

    section = extract_markdown_subsection(
        guide, heading="Build prerequisites for native extensions"
    )

    assert "is_rust_available()" in section, (
        "Missing documentation clause: 'is_rust_available()'"
    )


# -- 4.5.4: Troubleshooting ---------------------------------------------------


def test_users_guide_has_troubleshooting_section() -> None:
    """Users' guide should contain a troubleshooting heading."""
    guide = _read_doc("docs/users-guide.md")

    section = extract_markdown_subsection(guide, heading="Troubleshooting")

    assert section, "Troubleshooting section is empty"


def test_troubleshooting_covers_missing_wheels() -> None:
    """Troubleshooting section should address missing wheels."""
    guide = _read_doc("docs/users-guide.md")

    section = extract_markdown_subsection(guide, heading="Troubleshooting")

    assert "wheel" in section.lower(), (
        "Missing documentation clause about wheels in troubleshooting"
    )


def test_troubleshooting_covers_fallback_behaviour() -> None:
    """Troubleshooting section should address fallback behaviour."""
    guide = _read_doc("docs/users-guide.md")

    section = extract_markdown_subsection(guide, heading="Troubleshooting")

    assert "fallback" in section.lower(), (
        "Missing documentation clause about fallback in troubleshooting"
    )
    assert "CUPRUM_STREAM_BACKEND" in section, (
        "Missing documentation clause: 'CUPRUM_STREAM_BACKEND'"
    )


def test_troubleshooting_covers_benchmark_interpretation() -> None:
    """Troubleshooting section should address benchmark interpretation."""
    guide = _read_doc("docs/users-guide.md")

    section = extract_markdown_subsection(guide, heading="Troubleshooting")

    assert "benchmark" in section.lower(), (
        "Missing documentation clause about benchmarks in troubleshooting"
    )


def test_design_doc_states_correct_minimum_rust_version() -> None:
    """Design doc should state Rust 1.85+ as the minimum toolchain version."""
    design_doc = _read_doc("docs/cuprum-design.md")

    assert "rustc 1.85+" in design_doc or "Rust 1.85" in design_doc, (
        "Design doc should state Rust 1.85+ as the minimum toolchain version"
    )
