"""Behavioural tests for the Rust stream pump."""

from __future__ import annotations

import os
import typing as typ

from pytest_bdd import given, scenario, then, when

from tests.helpers.stream_pipes import _pipe_pair, _read_all, _safe_close

if typ.TYPE_CHECKING:
    from types import ModuleType


@scenario(
    "../features/rust_streams.feature",
    "Rust pump stream transfers data between pipes",
)
def test_rust_pump_stream_behaviour() -> None:
    """Validate the Rust pump stream behaviour."""


@given("the Rust pump stream is available", target_fixture="rust_pump")
def given_rust_pump(rust_streams: ModuleType) -> typ.Callable[[int, int], int]:
    """Expose the Rust pump stream function."""
    return rust_streams.rust_pump_stream


@when(
    "I pump a payload through the Rust stream",
    target_fixture="pumped_payload",
)
def when_pump_payload(
    rust_pump: typ.Callable[[int, int], int],
) -> tuple[bytes, bytes]:
    """Pump data through pipes using the Rust function."""
    payload = b"rust-pump-behaviour"
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        os.write(in_write, payload)
        _safe_close(in_write)

        rust_pump(in_read, out_write)

        _safe_close(out_write)
        output = _read_all(out_read)

    return payload, output


@then("the output matches the payload")
def then_payload_matches(pumped_payload: tuple[bytes, bytes]) -> None:
    """Assert the output matches the input payload."""
    payload, output = pumped_payload
    assert output == payload
