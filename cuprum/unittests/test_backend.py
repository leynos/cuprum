"""Unit tests for the stream backend dispatcher."""

from __future__ import annotations

import pytest

from cuprum import _rust_backend
from cuprum._backend import (
    StreamBackend,
    _check_rust_available,
    get_stream_backend,
)

_ENV_VAR = "CUPRUM_STREAM_BACKEND"


# -- StreamBackend enum -------------------------------------------------------


def test_stream_backend_enum_values() -> None:
    """Enum members have the expected lowercase string values."""
    assert StreamBackend.AUTO == "auto", "AUTO should equal 'auto'"
    assert StreamBackend.RUST == "rust", "RUST should equal 'rust'"
    assert StreamBackend.PYTHON == "python", "PYTHON should equal 'python'"


def test_stream_backend_is_str_enum() -> None:
    """StreamBackend members are plain strings."""
    assert isinstance(StreamBackend.AUTO, str), "AUTO should be a str instance"
    assert isinstance(StreamBackend.RUST, str), "RUST should be a str instance"
    assert isinstance(StreamBackend.PYTHON, str), "PYTHON should be a str instance"


# -- auto mode ----------------------------------------------------------------


def test_auto_returns_rust_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto mode selects Rust when the extension is available."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    monkeypatch.setattr(_rust_backend, "is_available", lambda: True)

    assert get_stream_backend() is StreamBackend.RUST, (
        "auto mode should select Rust when available"
    )


def test_auto_returns_python_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto mode falls back to Python when Rust is unavailable."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    monkeypatch.setattr(_rust_backend, "is_available", lambda: False)

    assert get_stream_backend() is StreamBackend.PYTHON, (
        "auto mode should fall back to Python when Rust is unavailable"
    )


def test_auto_falls_back_on_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto mode falls back to Python when the Rust probe raises ImportError."""
    monkeypatch.delenv(_ENV_VAR, raising=False)

    def _broken_probe() -> bool:
        msg = "broken native module"
        raise ImportError(msg)

    monkeypatch.setattr(_rust_backend, "is_available", _broken_probe)

    assert get_stream_backend() is StreamBackend.PYTHON, (
        "auto mode should fall back to Python when probe raises ImportError"
    )


# -- forced rust mode ---------------------------------------------------------


def test_forced_rust_returns_rust_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced Rust mode returns RUST when the extension is available."""
    monkeypatch.setenv(_ENV_VAR, "rust")
    monkeypatch.setattr(_rust_backend, "is_available", lambda: True)

    assert get_stream_backend() is StreamBackend.RUST, (
        "forced Rust mode should return RUST when available"
    )


def test_forced_rust_raises_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced Rust mode raises ImportError when the extension is missing."""
    monkeypatch.setenv(_ENV_VAR, "rust")
    monkeypatch.setattr(_rust_backend, "is_available", lambda: False)

    with pytest.raises(ImportError, match=_ENV_VAR):
        get_stream_backend()


# -- forced python mode -------------------------------------------------------


def test_forced_python_returns_python_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced Python mode returns PYTHON without consulting availability."""
    call_count = 0

    def _sentinel() -> bool:
        nonlocal call_count
        call_count += 1
        msg = "is_available must not be called in forced Python mode"
        raise AssertionError(msg)

    monkeypatch.setattr(_rust_backend, "is_available", _sentinel)
    monkeypatch.setenv(_ENV_VAR, "python")

    assert get_stream_backend() is StreamBackend.PYTHON, (
        "forced Python mode should return PYTHON"
    )
    assert call_count == 0, "is_available should not be called in forced Python mode"


# -- env var validation -------------------------------------------------------


def test_invalid_env_var_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized env var value raises ValueError."""
    monkeypatch.setenv(_ENV_VAR, "turbo")
    monkeypatch.setattr(_rust_backend, "is_available", lambda: False)

    with pytest.raises(ValueError, match="turbo"):
        get_stream_backend()


@pytest.mark.parametrize(
    ("rust_available_str", "expected"),
    [
        ("false", StreamBackend.PYTHON),
        ("true", StreamBackend.RUST),
    ],
    ids=["rust-unavailable", "rust-available"],
)
def test_empty_env_var_uses_auto_mode(
    monkeypatch: pytest.MonkeyPatch,
    rust_available_str: str,
    expected: StreamBackend,
) -> None:
    """An explicit empty env var value behaves like auto mode."""
    rust_available = rust_available_str == "true"
    monkeypatch.setenv(_ENV_VAR, "")
    monkeypatch.setattr(_rust_backend, "is_available", lambda: rust_available)

    assert get_stream_backend() is expected, (
        f"empty env var should behave like auto mode (expected {expected!r})"
    )


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("AUTO", StreamBackend.PYTHON),
        ("Rust", StreamBackend.RUST),
        ("PYTHON", StreamBackend.PYTHON),
        ("  auto  ", StreamBackend.PYTHON),
        ("auto", StreamBackend.RUST),
    ],
    ids=[
        "upper-auto",
        "mixed-rust",
        "upper-python",
        "whitespace-auto",
        "lower-auto-rust-available",
    ],
)
def test_env_var_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str,
    expected: StreamBackend,
) -> None:
    """Env var values are case-insensitive and whitespace-stripped."""
    monkeypatch.setenv(_ENV_VAR, env_value)
    # "Rust" and "auto" (rust-available) expect the extension to be present.
    is_rust = expected is StreamBackend.RUST
    monkeypatch.setattr(_rust_backend, "is_available", lambda: is_rust)

    assert get_stream_backend() is expected, (
        f"expected {expected!r} for env value {env_value!r}"
    )


# -- caching ------------------------------------------------------------------


def test_availability_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The availability probe is called at most once across invocations."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    call_count = 0

    def _counting_probe() -> bool:
        nonlocal call_count
        call_count += 1
        return False

    monkeypatch.setattr(_rust_backend, "is_available", _counting_probe)

    get_stream_backend()
    get_stream_backend()

    assert call_count == 1, "expected availability check to be called once"


def test_cache_clear_allows_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing the cache allows the availability probe to run again."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    monkeypatch.setattr(_rust_backend, "is_available", lambda: False)

    assert get_stream_backend() is StreamBackend.PYTHON, (
        "first call should return PYTHON when unavailable"
    )

    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()
    monkeypatch.setattr(_rust_backend, "is_available", lambda: True)

    assert get_stream_backend() is StreamBackend.RUST, (
        "after clearing cache, should return RUST when available"
    )
