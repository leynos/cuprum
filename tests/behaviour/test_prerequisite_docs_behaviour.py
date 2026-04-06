"""Behavioural tests for build prerequisite and troubleshooting documentation."""

from __future__ import annotations

from pytest_bdd import given, scenario, then, when

from tests.helpers import (
    BUILD_PREREQUISITES_HEADING,
    TROUBLESHOOTING_HEADING,
    contains_case_insensitive,
    extract_markdown_subsection,
    read_users_guide,
)


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
    guide = read_users_guide()
    return extract_markdown_subsection(guide, heading=BUILD_PREREQUISITES_HEADING)


@given(
    "the users' guide troubleshooting section",
    target_fixture="troubleshooting_section",
)
def given_troubleshooting_section() -> str:
    """Load the troubleshooting section from the users' guide."""
    guide = read_users_guide()
    return extract_markdown_subsection(guide, heading=TROUBLESHOOTING_HEADING)


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
    """Assert the guide mentions cargo and maturin.

    Uses case-insensitive checks to avoid brittle failures due to
    formatting or capitalization changes.
    """
    assert contains_case_insensitive(read_prerequisites, "cargo"), (
        f"expected 'cargo' to be present in read_prerequisites: {read_prerequisites!r}"
    )
    assert contains_case_insensitive(read_prerequisites, "maturin"), (
        "expected 'maturin' to be present in read_prerequisites: "
        f"{read_prerequisites!r}"
    )


@then("it mentions rustup for installation")
def then_mentions_rustup(read_prerequisites: str) -> None:
    """Assert the guide mentions rustup.

    Uses case-insensitive check to avoid brittle failures.
    """
    assert contains_case_insensitive(read_prerequisites, "rustup"), (
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
    """Assert the troubleshooting section covers missing wheels.

    Uses case-insensitive check for consistency with other tests.
    """
    assert contains_case_insensitive(read_troubleshooting, "wheel"), (
        "expected 'wheel' to be present in read_troubleshooting: "
        f"{read_troubleshooting!r}"
    )


@then("it explains forced fallback behaviour via CUPRUM_STREAM_BACKEND")
def then_explains_fallback(read_troubleshooting: str) -> None:
    """Assert the troubleshooting section covers fallback behaviour.

    Uses case-insensitive check for general term, exact match for env var.
    """
    assert contains_case_insensitive(read_troubleshooting, "fallback"), (
        "expected 'fallback' to be present in read_troubleshooting: "
        f"{read_troubleshooting!r}"
    )
    # Environment variable name must match exactly
    assert "CUPRUM_STREAM_BACKEND" in read_troubleshooting, (
        "expected 'CUPRUM_STREAM_BACKEND' to be present in read_troubleshooting: "
        f"{read_troubleshooting!r}"
    )


@then("it covers benchmark result interpretation")
def then_covers_benchmark_interpretation(read_troubleshooting: str) -> None:
    """Assert the troubleshooting section covers benchmark interpretation.

    Uses case-insensitive check for consistency with other tests.
    """
    assert contains_case_insensitive(read_troubleshooting, "benchmark"), (
        "expected 'benchmark' to be present in read_troubleshooting: "
        f"{read_troubleshooting!r}"
    )
