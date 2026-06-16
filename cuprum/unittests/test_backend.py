"""Unit tests for stream backend dispatch and Rust availability resolution.

The tests pin the contract between the raw Rust import probe, the cached
dispatch resolver, and the public Rust availability helper.
"""

from __future__ import annotations

import pytest

from cuprum import _rust_backend
from cuprum import rust as rust_api
from cuprum._backend import (
    StreamBackend,
    _check_rust_available,
    get_stream_backend,
    set_rust_availability_for_testing,
)
from cuprum.rust import is_rust_available

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
        """Raise ImportError to simulate a broken native backend probe."""
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
        """Fail if the availability probe is consulted in forced Python mode."""
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
        """Count each invocation of the availability probe."""
        nonlocal call_count
        call_count += 1
        return False

    monkeypatch.setattr(_rust_backend, "is_available", _counting_probe)

    get_stream_backend()
    get_stream_backend()

    assert call_count == 1, "expected availability check to be called once"


# -- public probe agrees with dispatch ----------------------------------------


@pytest.mark.parametrize("available", [True, False])
def test_public_probe_agrees_with_dispatch_resolver(
    monkeypatch: pytest.MonkeyPatch,
    *,
    available: bool,
) -> None:
    """The public ``is_rust_available`` agrees with the dispatch resolver.

    The public helper must delegate to the same resolver object imported by the
    public Rust module, not the raw Rust import probe.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to override the public module's resolver binding.
    available : bool
        Generated availability the resolver should report.
    """
    call_count = 0

    def _counting_resolver() -> bool:
        """Return deterministic availability and count public resolver calls."""
        nonlocal call_count
        call_count += 1
        return available

    monkeypatch.delenv(_ENV_VAR, raising=False)
    monkeypatch.setattr(rust_api, "_check_rust_available", _counting_resolver)

    for _ in range(2):
        assert is_rust_available() is available, (
            "public probe must return the dispatch resolver availability"
        )

    assert call_count == 2, "public probe should call the dispatch resolver each time"


def test_public_probe_honours_test_override() -> None:
    """The public probe reflects ``set_rust_availability_for_testing``."""
    _check_rust_available.cache_clear()
    try:
        set_rust_availability_for_testing(is_available=True)
        assert is_rust_available() is True, "override should force availability"

        set_rust_availability_for_testing(is_available=False)
        assert is_rust_available() is False, "override should force unavailability"
    finally:
        set_rust_availability_for_testing(is_available=None)


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
