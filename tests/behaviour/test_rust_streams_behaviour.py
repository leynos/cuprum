"""Behavioural tests for the Rust stream extension.

These BDD scenarios verify that the optional Rust-backed stream operations
behave as expected from a consumer perspective.

Example
-------
pytest tests/behaviour/test_rust_streams_behaviour.py -k rust_pump_stream_behaviour
"""

from __future__ import annotations

import os
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum.unittests._rust_stream_test_support import _set_binary_mode
from tests.helpers.consume_boundary_oracle import _OracleMetadata, check_payload
from tests.helpers.stream_pipes import (
    _pipe_pair,
    _read_all,
    _safe_close,
    pump_payload_through_pipes,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType


_pump_windows_only = pytest.mark.skipif(
    os.name == "nt",
    reason="pump still requires a synchronous handle capability",
)


def _expose_rust_stream_function(
    rust_streams: ModuleType,
    function_name: str,
) -> cabc.Callable[..., typ.Any]:
    """Return a named Rust stream function from the module.

    Parameters
    ----------
    rust_streams : ModuleType
        The Rust streams module fixture.
    function_name : str
        Name of the Rust stream function to expose.

    Returns
    -------
    Callable[..., Any]
        The requested Rust stream function.
    """
    return typ.cast(
        "cabc.Callable[..., typ.Any]",
        getattr(rust_streams, function_name),
    )


@_pump_windows_only
@scenario(
    "../features/rust_streams.feature",
    "Rust pump stream transfers data between pipes",
)
def test_rust_pump_stream_behaviour() -> None:
    """Validate the Rust pump stream behaviour.

    Parameters
    ----------
    None

    Returns
    -------
    None
    """


@scenario(
    "../features/rust_streams.feature",
    "Rust consume stream replaces invalid UTF-8",
)
def test_rust_consume_stream_behaviour() -> None:
    """Validate the Rust consume stream behaviour.

    Parameters
    ----------
    None

    Returns
    -------
    None
    """


@_pump_windows_only
@scenario(
    "../features/rust_streams.feature",
    "Rust pump stream handles large pipe transfers",
)
def test_rust_pump_stream_large_transfer_behaviour() -> None:
    """Validate the Rust pump stream handles large payloads.

    This scenario exercises the splice code path on Linux by transferring
    a payload larger than typical buffer sizes.

    Parameters
    ----------
    None

    Returns
    -------
    None
    """


@scenario(
    "../features/rust_streams.feature",
    "A large malformed stream preserves Python replacement decoding",
)
def test_rust_consume_stream_large_boundary_behaviour() -> None:
    """Compare Python, wrapper, direct, and subprocess pipe captures."""


@given("the Rust pump stream is available", target_fixture="rust_pump")
def given_rust_pump(rust_streams: ModuleType) -> cabc.Callable[[int, int], int]:
    """Expose the Rust pump stream function.

    Parameters
    ----------
    rust_streams : ModuleType
        The Rust streams module fixture.

    Returns
    -------
    Callable[[int, int], int]
        Function that pumps data between file descriptors.
    """
    return _expose_rust_stream_function(rust_streams, "rust_pump_stream")


@given("the Rust consume stream is available", target_fixture="rust_consume")
def given_rust_consume(
    rust_streams: ModuleType,
) -> cabc.Callable[..., str]:
    """Expose the Rust consume stream function.

    Parameters
    ----------
    rust_streams : ModuleType
        The Rust streams module fixture.

    Returns
    -------
    Callable[..., str]
        Function that consumes a stream from a file descriptor.
    """
    return _expose_rust_stream_function(rust_streams, "rust_consume_stream")


@given(
    "a payload of 1048577 bytes ending with an incomplete UTF-8 sequence",
    target_fixture="large_malformed_payload",
)
def given_large_malformed_payload() -> bytes:
    """Build the exact-size repeated-prefix payload with an incomplete tail."""
    length = 1_048_577
    tail = b"\xe2\x98"
    pattern = b"cuprum-boundary/"
    prefix_length = length - len(tail)
    prefix = (pattern * ((prefix_length + len(pattern) - 1) // len(pattern)))[
        :prefix_length
    ]
    return prefix + tail


@when(
    "the native and Python consumers capture the complete payload",
    target_fixture="large_capture_evidence",
)
def when_compare_large_consumers(
    rust_streams: ModuleType,
    large_malformed_payload: bytes,
) -> _OracleMetadata:
    """Run all boundary consumers, including a live subprocess pipe producer."""
    # The ``rust_streams`` fixture already skips when the extension is absent,
    # so reaching here proves the compiled backend is loaded.
    del rust_streams
    return check_payload(large_malformed_payload, buffer_size=65_535)


@then("both captures equal Python UTF-8 replacement decoding")
def then_large_captures_match_python(
    large_malformed_payload: bytes,
    large_capture_evidence: _OracleMetadata,
) -> None:
    """Assert each native capture matches Python's independent replacement."""
    expected = large_malformed_payload.decode("utf-8", errors="replace")
    assert len(large_capture_evidence.native_captures) == 6, (
        "wrapper, direct-native, and subprocess consumers must all run"
    )
    assert all(
        capture == expected for capture in large_capture_evidence.native_captures
    ), "every native capture must match Python's UTF-8 replacement output"


@then("omitted and explicit default buffers produce the same capture")
def then_large_buffer_defaults_match(
    large_capture_evidence: _OracleMetadata,
) -> None:
    """Assert wrapper and direct-native omitted defaults match explicit ones."""
    captures = large_capture_evidence.native_captures
    assert captures[0] == captures[1], "wrapper defaults must produce the same capture"
    assert captures[3] == captures[4], (
        "direct-native defaults must produce the same capture"
    )


@then("the caller's reader descriptor remains open")
def then_large_readers_remain_open(
    large_capture_evidence: _OracleMetadata,
) -> None:
    """Assert all borrowed reader descriptors survived their native calls."""
    assert large_capture_evidence.reader_open_checks == len(
        large_capture_evidence.native_captures
    ), "every native consumer must leave its caller-owned reader open"


@when(
    "I pump a payload through the Rust stream",
    target_fixture="pumped_payload",
)
def when_pump_payload(
    rust_pump: cabc.Callable[[int, int], int],
) -> tuple[bytes, bytes]:
    """Pump data through pipes using the Rust function.

    Parameters
    ----------
    rust_pump : Callable[[int, int], int]
        Rust pump function for transferring bytes.

    Returns
    -------
    tuple[bytes, bytes]
        The input payload and the output collected from the pipe.
    """
    payload = b"rust-pump-behaviour"
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        os.write(in_write, payload)
        _safe_close(in_write)

        rust_pump(in_read, out_write)

        _safe_close(out_write)
        output = _read_all(out_read)

    return payload, output


@when(
    "I consume a payload with invalid UTF-8",
    target_fixture="consumed_payload",
)
def when_consume_invalid_utf8(
    rust_consume: cabc.Callable[..., str],
) -> tuple[bytes, str]:
    """Consume invalid UTF-8 payload through pipes.

    Parameters
    ----------
    rust_consume : Callable[..., str]
        Rust consume function for decoding bytes.

    Returns
    -------
    tuple[bytes, str]
        The input payload and decoded output.
    """
    payload = b"rust-consume-\xff\xfe"
    read_fd, write_fd = os.pipe()
    open_write_fd: int | None = write_fd
    try:
        _set_binary_mode(write_fd)
        os.write(write_fd, payload)
        _safe_close(write_fd)
        open_write_fd = None
        output = rust_consume(read_fd, buffer_size=2)
    finally:
        _safe_close(read_fd)
        if open_write_fd is not None:
            _safe_close(open_write_fd)

    return payload, output


@then("the output matches the payload")
def then_payload_matches(pumped_payload: tuple[bytes, bytes]) -> None:
    """Assert the output matches the input payload.

    Parameters
    ----------
    pumped_payload : tuple[bytes, bytes]
        The input payload and the output captured from the pipe.
    """
    payload, output = pumped_payload
    assert output == payload, "expected output to match the pumped payload"


@then("the decoded output matches replacement semantics")
def then_output_matches_replacement(
    consumed_payload: tuple[bytes, str],
) -> None:
    """Assert invalid bytes are replaced using UTF-8 semantics.

    Parameters
    ----------
    consumed_payload : tuple[bytes, str]
        The input payload and decoded output.
    """
    payload, output = consumed_payload
    expected = payload.decode("utf-8", errors="replace")
    assert output == expected, "expected replacement semantics for invalid bytes"


@when(
    "I pump a large payload through the Rust stream",
    target_fixture="large_pumped_payload",
)
def when_pump_large_payload(
    rust_pump: cabc.Callable[[int, int], int],
) -> tuple[bytes, bytes, int]:
    """Pump a large payload through pipes using the Rust function.

    This exercises the splice code path on Linux by transferring a payload
    larger than the default buffer size. On non-Linux platforms, the
    read/write fallback is used.

    Parameters
    ----------
    rust_pump : Callable[[int, int], int]
        Rust pump function for transferring bytes.

    Returns
    -------
    tuple[bytes, bytes, int]
        The input payload, the output collected from the pipe, and bytes pumped.
    """
    # 1 MB payload to exercise splice with multiple chunks
    payload = bytes(range(256)) * (1024 * 1024 // 256)
    output, pumped_bytes = pump_payload_through_pipes(rust_pump, payload)
    return payload, output, pumped_bytes


@then("the output matches the large payload")
def then_large_payload_matches(large_pumped_payload: tuple[bytes, bytes, int]) -> None:
    """Assert the output matches the large input payload.

    Parameters
    ----------
    large_pumped_payload : tuple[bytes, bytes, int]
        The input payload, the output captured from the pipe, and bytes pumped.
    """
    payload, output, pumped_bytes = large_pumped_payload
    assert output == payload, "expected output to match the large pumped payload"
    assert pumped_bytes == len(payload), "expected pumped byte count to match payload"
