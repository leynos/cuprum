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


@pytest.fixture(autouse=True)
def _clear_backend_cache() -> None:
    """Clear the cached availability result between tests."""
    _check_rust_available.cache_clear()


# -- StreamBackend enum -------------------------------------------------------


def test_stream_backend_enum_values() -> None:
    """Enum members have the expected lowercase string values."""
    assert StreamBackend.AUTO == "auto"
    assert StreamBackend.RUST == "rust"
    assert StreamBackend.PYTHON == "python"


def test_stream_backend_is_str_enum() -> None:
    """StreamBackend members are plain strings."""
    assert isinstance(StreamBackend.AUTO, str)
    assert isinstance(StreamBackend.RUST, str)
    assert isinstance(StreamBackend.PYTHON, str)


# -- auto mode ----------------------------------------------------------------


def test_auto_returns_rust_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto mode selects Rust when the extension is available."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    monkeypatch.setattr(_rust_backend, "is_available", lambda: True)

    assert get_stream_backend() is StreamBackend.RUST


def test_auto_returns_python_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto mode falls back to Python when Rust is unavailable."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    monkeypatch.setattr(_rust_backend, "is_available", lambda: False)

    assert get_stream_backend() is StreamBackend.PYTHON


# -- forced rust mode ---------------------------------------------------------


def test_forced_rust_returns_rust_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced Rust mode returns RUST when the extension is available."""
    monkeypatch.setenv(_ENV_VAR, "rust")
    monkeypatch.setattr(_rust_backend, "is_available", lambda: True)

    assert get_stream_backend() is StreamBackend.RUST


def test_forced_rust_raises_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced Rust mode raises ImportError when the extension is missing."""
    monkeypatch.setenv(_ENV_VAR, "rust")
    monkeypatch.setattr(_rust_backend, "is_available", lambda: False)

    with pytest.raises(ImportError, match=_ENV_VAR):
        get_stream_backend()


# -- forced python mode -------------------------------------------------------


def test_forced_python_returns_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced Python mode returns PYTHON regardless of availability."""
    monkeypatch.setenv(_ENV_VAR, "python")
    # Availability should not matter — do not monkeypatch is_available.
    assert get_stream_backend() is StreamBackend.PYTHON


# -- env var validation -------------------------------------------------------


def test_invalid_env_var_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognised env var value raises ValueError."""
    monkeypatch.setenv(_ENV_VAR, "turbo")
    monkeypatch.setattr(_rust_backend, "is_available", lambda: False)

    with pytest.raises(ValueError, match="turbo"):
        get_stream_backend()


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("AUTO", StreamBackend.PYTHON),
        ("Rust", StreamBackend.RUST),
        ("PYTHON", StreamBackend.PYTHON),
        ("  auto  ", StreamBackend.PYTHON),
    ],
    ids=["upper-auto", "mixed-rust", "upper-python", "whitespace-auto"],
)
def test_env_var_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str,
    expected: StreamBackend,
) -> None:
    """Env var values are case-insensitive and whitespace-stripped."""
    monkeypatch.setenv(_ENV_VAR, env_value)
    # "Rust" expects the extension to be available.
    is_rust = expected is StreamBackend.RUST
    monkeypatch.setattr(_rust_backend, "is_available", lambda: is_rust)

    assert get_stream_backend() is expected


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

    assert get_stream_backend() is StreamBackend.PYTHON

    _check_rust_available.cache_clear()
    monkeypatch.setattr(_rust_backend, "is_available", lambda: True)

    assert get_stream_backend() is StreamBackend.RUST
