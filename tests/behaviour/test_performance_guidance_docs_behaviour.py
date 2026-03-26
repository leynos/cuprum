"""Behavioural tests for performance guidance documentation."""

from __future__ import annotations

from pathlib import Path

from pytest_bdd import given, scenario, then, when


@scenario(
    "../features/performance_guidance_docs.feature",
    "Users can find backend-selection guidance in the users' guide",
)
def test_users_can_find_backend_selection_guidance() -> None:
    """Users can find backend-selection guidance in the guide."""


@given(
    "the users' guide performance section",
    target_fixture="performance_guidance_section",
)
def given_performance_guidance_section() -> str:
    """Load the performance-guidance section from the users' guide."""
    guide_path = Path(__file__).resolve().parents[2] / "docs/users-guide.md"
    guide = guide_path.read_text(encoding="utf-8")
    _, section = guide.split("### Choosing a stream backend", maxsplit=1)
    return section


@when(
    "I read the backend-selection guidance",
    target_fixture="read_guidance",
)
def when_read_backend_selection_guidance(
    performance_guidance_section: str,
) -> str:
    """Return the section text as the read guidance."""
    return performance_guidance_section


@then("it explains when to use auto, python, and rust")
def then_explains_when_to_use_each_backend(read_guidance: str) -> None:
    """Assert the guide explains all three backend modes."""
    assert "`auto`" in read_guidance
    assert "`python`" in read_guidance
    assert "`rust`" in read_guidance


@then("it tells me to set CUPRUM_STREAM_BACKEND before first backend resolution")
def then_explains_when_to_set_env_var(read_guidance: str) -> None:
    """Assert the guide explains env-var timing."""
    assert "before first backend resolution in the process" in read_guidance


@then(
    "it explains that current Rust acceleration applies to inter-stage pumping, "
    "not stdout or stderr capture"
)
def then_explains_pumping_scope(read_guidance: str) -> None:
    """Assert the guide states the current pumping-versus-capture scope."""
    assert "inter-stage pipeline pumping" in read_guidance
    assert "stdout/stderr capture still uses the Python pathway" in read_guidance


@then("it points me to make benchmark-e2e for workload-specific measurement")
def then_points_to_measurement_command(read_guidance: str) -> None:
    """Assert the guide points readers to the benchmark command."""
    assert "`make benchmark-e2e`" in read_guidance
