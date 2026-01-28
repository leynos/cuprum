"""Behavioural tests for the Rust stream pump."""

from __future__ import annotations

import contextlib
import os
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum import _rust_backend

if typ.TYPE_CHECKING:
    from types import ModuleType


@scenario(
    "../features/rust_streams.feature",
    "Rust pump stream transfers data between pipes",
)
def test_rust_pump_stream_behaviour() -> None:
    """Validate the Rust pump stream behaviour."""


def _safe_close(fd: int) -> None:
    """Close a file descriptor, ignoring errors."""
    with contextlib.suppress(OSError):
        os.close(fd)


def _read_all(fd: int, *, chunk_size: int = 4096) -> bytes:
    """Read all data from a file descriptor until EOF."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, chunk_size)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _load_streams_rs() -> ModuleType:
    """Import the Rust streams module or skip if unavailable."""
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed.")
    from cuprum import _streams_rs

    return _streams_rs


@given("the Rust pump stream is available", target_fixture="rust_pump")
def given_rust_pump() -> typ.Callable[[int, int], int]:
    """Expose the Rust pump stream function."""
    streams = _load_streams_rs()
    return streams.rust_pump_stream


@when(
    "I pump a payload through the Rust stream",
    target_fixture="pumped_payload",
)
def when_pump_payload(
    rust_pump: typ.Callable[[int, int], int],
) -> tuple[bytes, bytes]:
    """Pump data through pipes using the Rust function."""
    payload = b"rust-pump-behaviour"
    in_read, in_write = os.pipe()
    out_read, out_write = os.pipe()

    try:
        os.write(in_write, payload)
        _safe_close(in_write)

        rust_pump(in_read, out_write)

        _safe_close(out_write)
        output = _read_all(out_read)
    finally:
        _safe_close(in_read)
        _safe_close(in_write)
        _safe_close(out_read)
        _safe_close(out_write)

    return payload, output


@then("the output matches the payload")
def then_payload_matches(pumped_payload: tuple[bytes, bytes]) -> None:
    """Assert the output matches the input payload."""
    payload, output = pumped_payload
    assert output == payload
