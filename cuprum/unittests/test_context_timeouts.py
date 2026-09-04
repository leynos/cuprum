"""Unit tests for execution-context timeout validation and resolution."""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum.context import (
    CuprumContext,
    ScopeConfig,
)
from cuprum.context._policy import (
    _resolve_narrowed_timeout,
    _validate_timeout,
)
from cuprum.sh import ExecutionContext
from cuprum.unittests import strategies as cuprum_st

# Run the symbolic backend for these tests with:
#   uv run pytest cuprum/unittests/test_context_timeouts.py -m crosshair \
#     --hypothesis-profile=crosshair

# =============================================================================
# CuprumContext Basics
# =============================================================================


_TIMEOUT_VALIDATION_CASES = [
    pytest.param(None, None, None, id="none-is-valid"),
    pytest.param(0.0, 0.0, None, id="zero-is-valid"),
    pytest.param(5.0, 5.0, None, id="positive-float-is-valid"),
    pytest.param(
        typ.cast("float", 5),
        5.0,
        None,
        id="positive-int-coerced-to-float",
    ),
    pytest.param(-1.0, None, r"timeout must be non-negative.*-1\.0", id="negative"),
    pytest.param(
        typ.cast("float", -5),
        None,
        r"timeout must be non-negative.*-5\.0",
        id="negative-int",
    ),
]


@pytest.mark.parametrize(
    ("timeout_input", "expected_result", "error_pattern"),
    _TIMEOUT_VALIDATION_CASES,
)
def test_scope_config_timeout_validation(
    timeout_input: float | None,
    expected_result: float | None,
    error_pattern: str | None,
) -> None:
    """ScopeConfig validates and coerces timeout values correctly."""
    if error_pattern is not None:
        with pytest.raises(ValueError, match=error_pattern):
            ScopeConfig(timeout=timeout_input)
    else:
        config = ScopeConfig(timeout=timeout_input)
        assert config.timeout == expected_result, (
            f"ScopeConfig(timeout={timeout_input!r}) must store "
            f"{expected_result!r}, got {config.timeout!r}"
        )
        if expected_result is not None:
            assert isinstance(config.timeout, float), (
                f"ScopeConfig(timeout={timeout_input!r}) must coerce the value "
                f"to float, got {type(config.timeout).__name__}"
            )


@pytest.mark.parametrize(
    ("timeout_input", "expected_result", "error_pattern"),
    _TIMEOUT_VALIDATION_CASES,
)
def test_cuprum_context_timeout_validation(
    timeout_input: float | None,
    expected_result: float | None,
    error_pattern: str | None,
) -> None:
    """CuprumContext validates and coerces timeout values correctly."""
    if error_pattern is not None:
        with pytest.raises(ValueError, match=error_pattern):
            CuprumContext(timeout=timeout_input)
    else:
        ctx = CuprumContext(timeout=timeout_input)
        assert ctx.timeout == expected_result, (
            f"CuprumContext(timeout={timeout_input!r}) must store "
            f"{expected_result!r}, got {ctx.timeout!r}"
        )
        if expected_result is not None:
            assert isinstance(ctx.timeout, float), (
                f"CuprumContext(timeout={timeout_input!r}) must coerce the "
                f"value to float, got {type(ctx.timeout).__name__}"
            )


@pytest.mark.crosshair
@cuprum_st.PROPERTY_SETTINGS
@given(timeout=st.none())
def test_validate_timeout_preserves_none(timeout: None) -> None:
    """Property: None timeout values are preserved."""
    assert _validate_timeout(timeout, "Test") is None, (
        "a None timeout must pass validation unchanged as None"
    )


@pytest.mark.crosshair
@cuprum_st.PROPERTY_SETTINGS
@given(timeout=st.floats(min_value=0.0, max_value=3600.0, allow_nan=False))
def test_validate_timeout_preserves_non_negative_floats(timeout: float) -> None:
    """Property: non-negative float timeouts are accepted unchanged."""
    result = _validate_timeout(timeout, "Test")

    assert result is not None, (
        f"validating the non-negative timeout {timeout!r} must not return None"
    )
    assert result >= timeout, (
        f"validated timeout {result!r} must not be less than the input {timeout!r}"
    )
    assert result <= timeout, (
        f"validated timeout {result!r} must not exceed the input {timeout!r}"
    )


@pytest.mark.crosshair
@cuprum_st.PROPERTY_SETTINGS
@given(timeout=st.integers(min_value=0, max_value=10**18))
def test_validate_timeout_coerces_non_negative_integers(timeout: int) -> None:
    """Property: non-negative integer timeouts are coerced to float."""
    result = _validate_timeout(typ.cast("float", timeout), "Test")

    assert result is not None, (
        f"validating the non-negative integer timeout {timeout!r} must not return None"
    )
    assert isinstance(result, float), (
        f"integer timeout {timeout!r} must be coerced to float, got "
        f"{type(result).__name__}"
    )
    assert result.hex() == float(timeout).hex(), (
        f"coerced timeout {result!r} must equal float({timeout!r}) exactly"
    )


@pytest.mark.crosshair
@cuprum_st.PROPERTY_SETTINGS
@given(
    timeout=st.floats(
        max_value=-0.000001,
        allow_infinity=False,
        allow_nan=False,
    )
)
def test_validate_timeout_rejects_negative_floats(timeout: float) -> None:
    """Property: negative float timeouts are rejected."""
    with pytest.raises(ValueError, match="timeout must be non-negative"):
        _validate_timeout(timeout, "Test")


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_validate_timeout_rejects_non_finite_values(timeout: float) -> None:
    """Non-finite timeout values are rejected before negative validation."""
    with pytest.raises(ValueError, match="timeout must be finite"):
        _validate_timeout(timeout, "Test")


def test_validate_timeout_rejects_integer_that_overflows_float() -> None:
    """Integer timeouts preserve the overflow cause under the value contract."""
    with pytest.raises(ValueError, match="timeout must be non-negative") as exc_info:
        _validate_timeout(typ.cast("float", 10**10_000), "Test")

    assert isinstance(exc_info.value.__cause__, OverflowError), (
        "an unrepresentable timeout must retain its OverflowError conversion cause"
    )


@pytest.mark.crosshair
@cuprum_st.PROPERTY_SETTINGS
@given(timeout=st.integers(max_value=-1))
def test_validate_timeout_rejects_negative_integers(timeout: int) -> None:
    """Property: negative integer timeouts are rejected."""
    with pytest.raises(ValueError, match="timeout must be non-negative"):
        _validate_timeout(typ.cast("float", timeout), "Test")


@pytest.mark.parametrize(
    "cleanup_grace",
    [
        pytest.param(0, id="zero"),
        pytest.param(0.25, id="fractional"),
    ],
)
def test_execution_context_validates_native_pump_cleanup_grace(
    cleanup_grace: float,
) -> None:
    """The native-pump grace accepts finite, non-negative values."""
    context = ExecutionContext(native_pump_cleanup_grace=cleanup_grace)

    assert context.native_pump_cleanup_grace == pytest.approx(float(cleanup_grace)), (
        "ExecutionContext must retain the validated cleanup grace, found "
        f"{context.native_pump_cleanup_grace!r}"
    )


@pytest.mark.parametrize("cleanup_grace", [float("inf"), -0.1])
def test_execution_context_rejects_invalid_native_pump_cleanup_grace(
    cleanup_grace: float,
) -> None:
    """The native-pump grace rejects non-finite and negative values."""
    with pytest.raises(ValueError, match="native_pump_cleanup_grace"):
        ExecutionContext(native_pump_cleanup_grace=cleanup_grace)


@pytest.mark.crosshair
@cuprum_st.PROPERTY_SETTINGS
@given(parent=cuprum_st.timeouts())
def test_resolve_narrowed_timeout_inherits_without_config(
    parent: float | None,
) -> None:
    """Property: absent config timeout inherits the parent timeout."""
    assert _resolve_narrowed_timeout(parent, None) == parent, (
        f"a None config timeout must inherit the parent timeout {parent!r}"
    )


@pytest.mark.crosshair
@cuprum_st.PROPERTY_SETTINGS
@given(
    parent=cuprum_st.timeouts(),
    config=st.floats(min_value=0.0, max_value=3600.0, allow_nan=False),
)
def test_resolve_narrowed_timeout_config_overrides_parent(
    parent: float | None,
    config: float,
) -> None:
    """Property: explicit config timeout overrides any parent timeout."""
    assert _resolve_narrowed_timeout(parent, config) == config, (
        f"an explicit config timeout {config!r} must override the parent "
        f"timeout {parent!r}"
    )
