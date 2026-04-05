"""Documentation contract tests for roadmap items 4.5.3 and 4.5.4."""

from __future__ import annotations

import pytest

from tests.helpers import (
    BUILD_PREREQUISITES_HEADING,
    TROUBLESHOOTING_HEADING,
    contains_case_insensitive,
    extract_markdown_subsection,
    read_doc,
    read_users_guide,
)

# -- Fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def users_guide() -> str:
    """Load the users' guide once per module."""
    return read_users_guide()


@pytest.fixture(scope="module")
def prerequisites_section(users_guide: str) -> str:
    """Extract the build prerequisites section from the users' guide."""
    return extract_markdown_subsection(users_guide, heading=BUILD_PREREQUISITES_HEADING)


@pytest.fixture(scope="module")
def troubleshooting_section(users_guide: str) -> str:
    """Extract the troubleshooting section from the users' guide."""
    return extract_markdown_subsection(users_guide, heading=TROUBLESHOOTING_HEADING)


# -- 4.5.3: Build prerequisites -----------------------------------------------


@pytest.mark.parametrize(
    "term",
    [
        "Rust",
        "maturin",
        "cargo",
        "rustup",
    ],
)
def test_users_guide_build_prerequisites_mentions_tools(
    prerequisites_section: str, term: str
) -> None:
    """Users' guide build prerequisites should mention essential tools.

    Uses case-insensitive checks to avoid brittle failures from capitalization.
    """
    assert contains_case_insensitive(prerequisites_section, term), (
        f"Missing documentation clause: '{term}'"
    )


def test_users_guide_build_prerequisites_mentions_version(
    prerequisites_section: str,
) -> None:
    """Users' guide should specify the minimum Rust version."""
    assert "1.85" in prerequisites_section, "Missing documentation clause: '1.85'"


def test_users_guide_build_prerequisites_mentions_maturin_develop(
    prerequisites_section: str,
) -> None:
    """Users' guide should mention the maturin develop command."""
    assert "`maturin develop`" in prerequisites_section, (
        "Missing documentation clause: '`maturin develop`'"
    )


def test_users_guide_build_prerequisites_mention_verification(
    prerequisites_section: str,
) -> None:
    """Users' guide should tell readers how to verify the Rust extension."""
    assert "is_rust_available()" in prerequisites_section, (
        "Missing documentation clause: 'is_rust_available()'"
    )


# -- 4.5.4: Troubleshooting ---------------------------------------------------


def test_users_guide_has_troubleshooting_section(troubleshooting_section: str) -> None:
    """Users' guide should contain a troubleshooting heading."""
    assert troubleshooting_section.strip(), "Troubleshooting section is empty"


@pytest.mark.parametrize(
    "term",
    [
        "wheel",
        "fallback",
        "benchmark",
    ],
)
def test_troubleshooting_covers_topics(troubleshooting_section: str, term: str) -> None:
    """Troubleshooting section should address key topics.

    Uses case-insensitive checks to avoid brittle failures from capitalization.
    """
    assert contains_case_insensitive(troubleshooting_section, term), (
        f"Missing documentation clause about {term} in troubleshooting"
    )


def test_troubleshooting_mentions_backend_env_var(
    troubleshooting_section: str,
) -> None:
    """Troubleshooting section should mention CUPRUM_STREAM_BACKEND."""
    assert "CUPRUM_STREAM_BACKEND" in troubleshooting_section, (
        "Missing documentation clause: 'CUPRUM_STREAM_BACKEND'"
    )


def test_design_doc_states_correct_minimum_rust_version() -> None:
    """Design doc should state Rust 1.85+ as the minimum toolchain version."""
    design_doc = read_doc("docs/cuprum-design.md")

    assert "rustc 1.85+" in design_doc or "Rust 1.85" in design_doc, (
        "Design doc should state Rust 1.85+ as the minimum toolchain version"
    )
