"""Documentation contract tests for roadmap items 4.5.3 and 4.5.4."""

from __future__ import annotations

from tests.helpers import (
    BUILD_PREREQUISITES_HEADING,
    TROUBLESHOOTING_HEADING,
    contains_case_insensitive,
    extract_markdown_subsection,
    read_doc,
    read_users_guide,
)

# -- 4.5.3: Build prerequisites -----------------------------------------------


def test_users_guide_has_build_prerequisites_section() -> None:
    """Users' guide should contain a build prerequisites heading."""
    guide = read_users_guide()

    section = extract_markdown_subsection(guide, heading=BUILD_PREREQUISITES_HEADING)

    # Case-insensitive checks for tool names to avoid brittle failures
    assert contains_case_insensitive(section, "Rust"), (
        "Missing documentation clause: 'Rust'"
    )
    assert contains_case_insensitive(section, "maturin"), (
        "Missing documentation clause: 'maturin'"
    )
    assert contains_case_insensitive(section, "cargo"), (
        "Missing documentation clause: 'cargo'"
    )
    # Version and exact code references need exact matches
    assert "1.85" in section, "Missing documentation clause: '1.85'"
    assert "`maturin develop`" in section, (
        "Missing documentation clause: '`maturin develop`'"
    )


def test_users_guide_build_prerequisites_mention_rustup() -> None:
    """Users' guide should mention rustup as the installation method."""
    guide = read_users_guide()

    section = extract_markdown_subsection(guide, heading=BUILD_PREREQUISITES_HEADING)

    # Case-insensitive check for rustup
    assert contains_case_insensitive(section, "rustup"), (
        "Missing documentation clause: 'rustup'"
    )


def test_users_guide_build_prerequisites_mention_verification() -> None:
    """Users' guide should tell readers how to verify the Rust extension."""
    guide = read_users_guide()

    section = extract_markdown_subsection(guide, heading=BUILD_PREREQUISITES_HEADING)

    # Exact match required for function name
    assert "is_rust_available()" in section, (
        "Missing documentation clause: 'is_rust_available()'"
    )


# -- 4.5.4: Troubleshooting ---------------------------------------------------


def test_users_guide_has_troubleshooting_section() -> None:
    """Users' guide should contain a troubleshooting heading."""
    guide = read_users_guide()

    section = extract_markdown_subsection(guide, heading=TROUBLESHOOTING_HEADING)

    assert section, "Troubleshooting section is empty"


def test_troubleshooting_covers_missing_wheels() -> None:
    """Troubleshooting section should address missing wheels."""
    guide = read_users_guide()

    section = extract_markdown_subsection(guide, heading=TROUBLESHOOTING_HEADING)

    # Case-insensitive check using shared helper
    assert contains_case_insensitive(section, "wheel"), (
        "Missing documentation clause about wheels in troubleshooting"
    )


def test_troubleshooting_covers_fallback_behaviour() -> None:
    """Troubleshooting section should address fallback behaviour."""
    guide = read_users_guide()

    section = extract_markdown_subsection(guide, heading=TROUBLESHOOTING_HEADING)

    # Case-insensitive check for general term
    assert contains_case_insensitive(section, "fallback"), (
        "Missing documentation clause about fallback in troubleshooting"
    )
    # Exact match required for environment variable name
    assert "CUPRUM_STREAM_BACKEND" in section, (
        "Missing documentation clause: 'CUPRUM_STREAM_BACKEND'"
    )


def test_troubleshooting_covers_benchmark_interpretation() -> None:
    """Troubleshooting section should address benchmark interpretation."""
    guide = read_users_guide()

    section = extract_markdown_subsection(guide, heading=TROUBLESHOOTING_HEADING)

    # Case-insensitive check using shared helper
    assert contains_case_insensitive(section, "benchmark"), (
        "Missing documentation clause about benchmarks in troubleshooting"
    )


def test_design_doc_states_correct_minimum_rust_version() -> None:
    """Design doc should state Rust 1.85+ as the minimum toolchain version."""
    design_doc = read_doc("docs/cuprum-design.md")

    assert "rustc 1.85+" in design_doc or "Rust 1.85" in design_doc, (
        "Design doc should state Rust 1.85+ as the minimum toolchain version"
    )
