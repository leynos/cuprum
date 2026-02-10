"""Behavioural tests for the Rust stream extension.

These BDD scenarios verify that the optional Rust-backed stream operations
behave as expected from a consumer perspective.

Example
-------
pytest tests/behaviour/test_rust_streams_behaviour.py -k rust_pump_stream_behaviour
"""

from __future__ import annotations

import os
import threading
import typing as typ

from pytest_bdd import given, scenario, then, when

from tests.helpers.stream_pipes import _pipe_pair, _read_all, _safe_close

if typ.TYPE_CHECKING:
    from types import ModuleType


def _expose_rust_stream_function(
    rust_streams: ModuleType,
    function_name: str,
) -> typ.Callable[..., typ.Any]:
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
        "typ.Callable[..., typ.Any]",
        getattr(rust_streams, function_name),
    )


@scenario(
    "../features/rust_streams.feature",
    "Rust pump stream transfers data between pipes",
)
def test_rust_pump_stream_behaviour() -> None:
    """Validate the Rust pump stream behavior.

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


@given("the Rust pump stream is available", target_fixture="rust_pump")
def given_rust_pump(rust_streams: ModuleType) -> typ.Callable[[int, int], int]:
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
) -> typ.Callable[..., str]:
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


@when(
    "I pump a payload through the Rust stream",
    target_fixture="pumped_payload",
)
def when_pump_payload(
    rust_pump: typ.Callable[[int, int], int],
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
    rust_consume: typ.Callable[..., str],
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

    Returns
    -------
    None
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

    Returns
    -------
    None
    """
    payload, output = consumed_payload
    expected = payload.decode("utf-8", errors="replace")
    assert output == expected, "expected replacement semantics for invalid bytes"


@when(
    "I pump a large payload through the Rust stream",
    target_fixture="large_pumped_payload",
)
def when_pump_large_payload(
    rust_pump: typ.Callable[[int, int], int],
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
    with _pipe_pair() as (in_read, in_write, out_read, out_write):
        # Write in a separate thread to avoid deadlock on full pipe buffer

        def writer() -> None:
            view = memoryview(payload)
            while view:
                written = os.write(in_write, view)
                view = view[written:]
            _safe_close(in_write)

        write_thread = threading.Thread(target=writer)
        write_thread.start()

        pumped_bytes = rust_pump(in_read, out_write)

        _safe_close(out_write)
        output = _read_all(out_read)
        write_thread.join()

    return payload, output, pumped_bytes


@then("the output matches the large payload")
def then_large_payload_matches(large_pumped_payload: tuple[bytes, bytes, int]) -> None:
    """Assert the output matches the large input payload.

    Parameters
    ----------
    large_pumped_payload : tuple[bytes, bytes, int]
        The input payload, the output captured from the pipe, and bytes pumped.

    Returns
    -------
    None
    """
    payload, output, pumped_bytes = large_pumped_payload
    assert output == payload, "expected output to match the large pumped payload"
    assert pumped_bytes == len(payload), "expected pumped byte count to match payload"
