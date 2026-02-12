"""Behavioural tests for the stream backend dispatcher."""

from __future__ import annotations

import pytest
from pytest_bdd import given, parsers, scenario, then, when

from cuprum import _rust_backend
from cuprum._backend import (
    StreamBackend,
    _check_rust_available,
    get_stream_backend,
)

_ENV_VAR = "CUPRUM_STREAM_BACKEND"


@pytest.fixture(autouse=True)
def _clear_backend_cache() -> None:
    """Clear the cached availability and backend results between scenarios."""
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()


# -- Scenarios ----------------------------------------------------------------


@scenario(
    "../features/backend_dispatcher.feature",
    "Auto mode selects Python when Rust is unavailable",
)
def test_auto_selects_python() -> None:
    """Auto mode falls back to Python when Rust is missing."""


@scenario(
    "../features/backend_dispatcher.feature",
    "Forced Rust mode raises when extension is unavailable",
)
def test_forced_rust_raises() -> None:
    """Forced Rust mode raises ImportError."""


@scenario(
    "../features/backend_dispatcher.feature",
    "Forced Python mode always selects Python",
)
def test_forced_python() -> None:
    """Forced Python mode returns PYTHON."""


# -- Given steps --------------------------------------------------------------


@given(
    "the Rust extension is not available",
    target_fixture="rust_unavailable",
)
def given_rust_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the Rust extension being absent."""
    monkeypatch.setattr(_rust_backend, "is_available", lambda: False)


@given(
    "the stream backend environment variable is unset",
    target_fixture="env_unset",
)
def given_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the backend environment variable is not set."""
    monkeypatch.delenv(_ENV_VAR, raising=False)


@given(
    parsers.parse("the stream backend is forced to {backend}"),
    target_fixture="env_forced",
)
def given_env_forced(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    """Set the backend environment variable to a specific value."""
    monkeypatch.setenv(_ENV_VAR, backend)


# -- When steps ---------------------------------------------------------------


@when(
    "I resolve the stream backend",
    target_fixture="resolved_backend",
)
def when_resolve_backend() -> StreamBackend:
    """Invoke the dispatcher."""
    return get_stream_backend()


@when(
    "I attempt to resolve the stream backend",
    target_fixture="resolve_error",
)
def when_attempt_resolve_backend() -> BaseException | None:
    """Invoke the dispatcher and capture any exception."""
    try:
        get_stream_backend()
    except (ImportError, ValueError) as exc:
        return exc
    return None


# -- Then steps ---------------------------------------------------------------


@then(parsers.parse("the resolved backend is {expected}"))
def then_resolved_backend(
    resolved_backend: StreamBackend,
    expected: str,
) -> None:
    """Assert the resolved backend matches the expected value."""
    assert resolved_backend == StreamBackend(expected), (
        f"expected {expected}, got {resolved_backend.value}"
    )


@then("an ImportError is raised")
def then_import_error_raised(resolve_error: BaseException | None) -> None:
    """Assert that an ImportError was raised."""
    assert isinstance(resolve_error, ImportError), (
        f"expected ImportError, got {type(resolve_error).__name__}: {resolve_error}"
    )
