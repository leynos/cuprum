"""Behavioural tests for the optional Rust extension probe."""

from __future__ import annotations

import importlib
import typing as typ

from pytest_bdd import given, scenario, then, when

import cuprum as c

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@scenario(
    "../features/rust_extension.feature",
    "Rust extension availability is discoverable",
)
def test_rust_extension_availability() -> None:
    """Behavioural coverage for the Rust availability probe."""


@scenario(
    "../features/rust_extension.feature",
    "Rust availability rejects a non-boolean resolver result",
)
def test_rust_extension_availability_rejects_non_boolean_result() -> None:
    """Behavioural coverage for invalid Rust availability results."""


@given("the Cuprum Rust availability probe", target_fixture="probe")
def given_probe() -> cabc.Callable[[], bool]:
    """Expose the Rust backend availability probe.

    Returns
    -------
    cabc.Callable[[], bool]
        The ``is_rust_available`` probe callable.
    """
    return c.is_rust_available


@given("a Rust availability resolver returning a non-boolean value")
def given_invalid_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the resolver with a non-Boolean result for this scenario."""

    def _invalid_resolver() -> object:
        """Return a deliberately invalid availability value."""
        return "available"

    monkeypatch.setattr(rust_api, "_check_rust_available", _invalid_resolver)


@when(
    "I check whether the Rust extension is available",
    target_fixture="availability",
)
def when_check_availability(probe: cabc.Callable[[], bool]) -> bool:
    """Invoke the availability probe.

    Parameters
    ----------
    probe : cabc.Callable[[], bool]
        The callable that probes whether the Rust extension is available.

    Returns
    -------
    bool
        Whether the Rust backend reports itself available.
    """
    return probe()


@when(
    "I check the invalid Rust availability result",
    target_fixture="availability_error",
)
def when_check_invalid_availability() -> TypeError:
    """Capture the runtime contract violation raised by the public probe."""
    with pytest.raises(TypeError) as error:
        c.is_rust_available()
    return error.value


@then("the probe returns a boolean")
def then_probe_returns_boolean(availability: object) -> None:
    """Assert the availability probe returns a boolean value."""
    assert isinstance(availability, bool), "Expected isinstance(availability, bool)"


@then("the probe rejects the result with TypeError")
def then_probe_rejects_invalid_result(availability_error: Exception) -> None:
    """Assert the public probe rejects the invalid resolver result."""
    assert isinstance(availability_error, TypeError), "Expected a TypeError"


@then("the probe agrees with the native module when it is installed")
def then_probe_matches_native(availability: object) -> None:
    """Assert the probe matches the native module when installed.

    Parameters
    ----------
    availability : object
        The availability result reported by the probe.

    Raises
    ------
    ImportError
        If importing the native module fails for a reason other than the
        module being absent.
    """
    try:
        native = importlib.import_module("cuprum._rust_backend_native")
    except ImportError as exc:
        if isinstance(exc, ModuleNotFoundError) and exc.name == (
            "cuprum._rust_backend_native"
        ):
            return
        raise
    assert availability is native.is_available(), (
        "Expected availability is native.is_available()"
    )
