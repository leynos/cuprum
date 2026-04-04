"""Behavioural tests for build prerequisite and troubleshooting documentation."""

from __future__ import annotations

from pathlib import Path

from pytest_bdd import given, scenario, then, when

from tests.helpers import extract_markdown_subsection


@scenario(
    "../features/prerequisite_docs.feature",
    "Users' guide documents Rust build prerequisites",
)
def test_users_guide_documents_rust_build_prerequisites() -> None:
    """Users' guide documents Rust build prerequisites."""


@scenario(
    "../features/prerequisite_docs.feature",
    "Users' guide includes troubleshooting guidance",
)
def test_users_guide_includes_troubleshooting_guidance() -> None:
    """Users' guide includes troubleshooting guidance."""


# -- Given steps ---------------------------------------------------------------


@given(
    "the users' guide build prerequisites section",
    target_fixture="prerequisites_section",
)
def given_build_prerequisites_section() -> str:
    """Load the build prerequisites section from the users' guide."""
    guide_path = Path(__file__).resolve().parents[2] / "docs/users-guide.md"
    guide = guide_path.read_text(encoding="utf-8")
    return extract_markdown_subsection(
        guide, heading="Build prerequisites for native extensions"
    )


@given(
    "the users' guide troubleshooting section",
    target_fixture="troubleshooting_section",
)
def given_troubleshooting_section() -> str:
    """Load the troubleshooting section from the users' guide."""
    guide_path = Path(__file__).resolve().parents[2] / "docs/users-guide.md"
    guide = guide_path.read_text(encoding="utf-8")
    return extract_markdown_subsection(guide, heading="Troubleshooting")


# -- When steps ----------------------------------------------------------------


@when(
    "I read the build prerequisites",
    target_fixture="read_prerequisites",
)
def when_read_build_prerequisites(prerequisites_section: str) -> str:
    """Return the section text as the read prerequisites."""
    return prerequisites_section


@when(
    "I read the troubleshooting guidance",
    target_fixture="read_troubleshooting",
)
def when_read_troubleshooting_guidance(troubleshooting_section: str) -> str:
    """Return the section text as the read troubleshooting guidance."""
    return troubleshooting_section


# -- Then steps (prerequisites) ------------------------------------------------


@then("it mentions Rust 1.85 or later")
def then_mentions_rust_version(read_prerequisites: str) -> None:
    """Assert the guide mentions the correct Rust version."""
    assert "1.85" in read_prerequisites, (
        f"expected '1.85' to be present in read_prerequisites: {read_prerequisites!r}"
    )


@then("it mentions cargo and maturin")
def then_mentions_cargo_and_maturin(read_prerequisites: str) -> None:
    """Assert the guide mentions cargo and maturin."""
    assert "cargo" in read_prerequisites, (
        f"expected 'cargo' to be present in read_prerequisites: {read_prerequisites!r}"
    )
    assert "maturin" in read_prerequisites, (
        "expected 'maturin' to be present in read_prerequisites: "
        f"{read_prerequisites!r}"
    )


@then("it mentions rustup for installation")
def then_mentions_rustup(read_prerequisites: str) -> None:
    """Assert the guide mentions rustup."""
    assert "rustup" in read_prerequisites, (
        f"expected 'rustup' to be present in read_prerequisites: {read_prerequisites!r}"
    )


@then("it explains how to verify the Rust extension")
def then_explains_verification(read_prerequisites: str) -> None:
    """Assert the guide explains how to verify the extension."""
    assert "is_rust_available()" in read_prerequisites, (
        "expected 'is_rust_available()' to be present in read_prerequisites: "
        f"{read_prerequisites!r}"
    )


# -- Then steps (troubleshooting) ----------------------------------------------


@then("it addresses missing wheels on unsupported platforms")
def then_addresses_missing_wheels(read_troubleshooting: str) -> None:
    """Assert the troubleshooting section covers missing wheels."""
    assert "wheel" in read_troubleshooting.lower(), (
        "expected 'wheel' to be present in read_troubleshooting: "
        f"{read_troubleshooting!r}"
    )


@then("it explains forced fallback behaviour via CUPRUM_STREAM_BACKEND")
def then_explains_fallback(read_troubleshooting: str) -> None:
    """Assert the troubleshooting section covers fallback behaviour."""
    assert "fallback" in read_troubleshooting.lower(), (
        "expected 'fallback' to be present in read_troubleshooting: "
        f"{read_troubleshooting!r}"
    )
    assert "CUPRUM_STREAM_BACKEND" in read_troubleshooting, (
        "expected 'CUPRUM_STREAM_BACKEND' to be present in read_troubleshooting: "
        f"{read_troubleshooting!r}"
    )


@then("it covers benchmark result interpretation")
def then_covers_benchmark_interpretation(read_troubleshooting: str) -> None:
    """Assert the troubleshooting section covers benchmark interpretation."""
    assert "benchmark" in read_troubleshooting.lower(), (
        "expected 'benchmark' to be present in read_troubleshooting: "
        f"{read_troubleshooting!r}"
    )
