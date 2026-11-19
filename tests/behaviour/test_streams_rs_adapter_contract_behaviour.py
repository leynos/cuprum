"""Behavioural coverage for the shared stream adapter contract.

The unit tables pin the policy against doubles. These scenarios exercise the
same policy through the compiled boundary, where the order and the exact
message a caller sees are observable. The buffer-first precedence is the
compatibility decision recorded for issue #659; the descriptor rows show that
pump and consume now classify the same raw value identically.

Example
-------
pytest tests/behaviour/test_streams_rs_adapter_contract_behaviour.py
"""

from __future__ import annotations

import contextlib
import os
import typing as typ

import pytest
from pytest_bdd import given, parsers, scenario, then, when

from cuprum.unittests._rust_stream_test_support import _safe_close

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType


class _IndexOnly:
    """Expose an integer through ``__index__`` and count conversions."""

    def __init__(self, value: int) -> None:
        self.value = value
        self.calls = 0

    def __index__(self) -> int:
        """Return the wrapped integer, recording the conversion."""
        self.calls += 1
        return self.value


class _IndexEvidence(typ.NamedTuple):
    """The result and conversion counts of one index-like call."""

    result: object
    calls: tuple[int, ...]


@scenario(
    "../features/rust_streams.feature",
    "The buffer size is the first reported stream argument failure",
)
def test_buffer_size_is_reported_first() -> None:
    """An invalid size is named even when the descriptor is also unusable."""


@scenario(
    "../features/rust_streams.feature",
    "Both adapters apply the same raw descriptor policy",
)
def test_shared_raw_descriptor_policy() -> None:
    """Pump and consume classify the same raw descriptor identically."""


@scenario(
    "../features/rust_streams.feature",
    "Index-like stream arguments are accepted by both entry points",
)
def test_index_like_arguments_are_accepted() -> None:
    """Each adapter converts every index-like argument exactly once."""


@given(
    "the stream adapter entry points are available",
    target_fixture="adapters",
)
def given_adapters_available(rust_streams: ModuleType) -> ModuleType:
    """Require the compiled-backed adapter module for the scenario."""
    for name in ("rust_pump_stream", "rust_consume_stream"):
        assert hasattr(rust_streams, name), (
            f"the stream adapter module must expose {name}"
        )
    return rust_streams


def _entry_point(adapters: ModuleType, operation: str) -> cabc.Callable[..., object]:
    """Return the named adapter entry point."""
    calls = {
        "pump": adapters.rust_pump_stream,
        "consume": adapters.rust_consume_stream,
    }
    entry_point = calls.get(operation)
    assert entry_point is not None, f"unknown operation {operation!r}"
    return typ.cast("cabc.Callable[..., object]", entry_point)


def _call_with_descriptor(
    adapters: ModuleType,
    operation: str,
    descriptor: int,
    buffer_size: int,
) -> BaseException:
    """Call the entry point so its argument validation raises."""
    entry_point = _entry_point(adapters, operation)
    with contextlib.ExitStack() as stack:
        args: tuple[int, ...] = (descriptor,)
        if operation == "pump":
            # The wrapper closes a writer that never reaches native ownership;
            # the ExitStack close is a suppressed no-op in that case.
            writer = os.open(os.devnull, os.O_WRONLY)
            stack.callback(_safe_close, writer)
            args = (descriptor, writer)
        with pytest.raises(BaseException) as excinfo:  # ruff: ignore[pytest-raises-too-broad] - the Then step asserts the class.
            entry_point(*args, buffer_size=buffer_size)
    return excinfo.value


@when(
    parsers.parse(
        "the {operation} entry point receives descriptor {descriptor:d} "
        "and buffer size {buffer_size:d}"
    ),
    target_fixture="raised_exception",
)
def when_entry_point_receives(
    adapters: ModuleType,
    operation: str,
    descriptor: int,
    buffer_size: int,
) -> BaseException:
    """Invoke the named entry point with the row's arguments."""
    return _call_with_descriptor(adapters, operation, descriptor, buffer_size)


@then(parsers.parse('it raises {exception} saying "{message}"'))
def then_raises_exactly(
    raised_exception: BaseException, exception: str, message: str
) -> None:
    """Assert the exact class and message the contract promises."""
    expected = {
        "OverflowError": OverflowError,
        "TypeError": TypeError,
        "ValueError": ValueError,
    }.get(exception)
    assert expected is not None, f"unknown exception {exception!r}"
    assert isinstance(raised_exception, expected), (
        f"expected {exception}, found {type(raised_exception).__name__}: "
        f"{raised_exception}"
    )
    assert str(raised_exception) == message, (
        f"expected the message {message!r}, found {str(raised_exception)!r}"
    )


def _feed_pipe(reader_fd: int, writer_fd: int, payload: bytes) -> None:
    """Write the payload and close the source so the reader observes EOF."""
    os.write(writer_fd, payload)
    _safe_close(writer_fd)


@when(
    "I pump a payload with index-like stream arguments",
    target_fixture="pump_evidence",
)
def when_pump_index_like(adapters: ModuleType) -> _IndexEvidence:
    """Pump through index-like reader, writer, and buffer arguments."""
    if os.name == "nt":
        pytest.skip("the Python pump boundary rejects raw Windows handles")

    payload = b"index-like-pump"
    with contextlib.ExitStack() as stack:
        reader_fd, upstream_fd = os.pipe()
        stack.callback(_safe_close, reader_fd)
        stack.callback(_safe_close, upstream_fd)
        sink_fd = os.open(os.devnull, os.O_WRONLY)
        stack.callback(_safe_close, sink_fd)
        _feed_pipe(reader_fd, upstream_fd, payload)

        reader = _IndexOnly(reader_fd)
        sink = _IndexOnly(sink_fd)
        size = _IndexOnly(1024)
        moved = adapters.rust_pump_stream(
            typ.cast("int", reader),
            typ.cast("int", sink),
            buffer_size=typ.cast("int", size),
        )

    assert moved == len(payload), f"the pump must transfer every byte, found {moved!r}"
    return _IndexEvidence(moved, (reader.calls, sink.calls, size.calls))


@when(
    "I consume a payload with index-like stream arguments",
    target_fixture="consume_evidence",
)
def when_consume_index_like(adapters: ModuleType) -> _IndexEvidence:
    """Consume through index-like reader and buffer arguments."""
    payload = b"index-like-consume"
    with contextlib.ExitStack() as stack:
        reader_fd, upstream_fd = os.pipe()
        stack.callback(_safe_close, reader_fd)
        stack.callback(_safe_close, upstream_fd)
        _feed_pipe(reader_fd, upstream_fd, payload)

        reader = _IndexOnly(reader_fd)
        size = _IndexOnly(8)
        output = adapters.rust_consume_stream(
            typ.cast("int", reader),
            buffer_size=typ.cast("int", size),
        )

    assert output == payload.decode(), (
        f"the consume must decode the whole payload, found {output!r}"
    )
    return _IndexEvidence(output, (reader.calls, size.calls))


@then("each entry point returns its payload and converted every argument once")
def then_index_like_evidence(
    pump_evidence: _IndexEvidence,
    consume_evidence: _IndexEvidence,
) -> None:
    """Both adapters normalized each index-like argument exactly once."""
    assert pump_evidence.calls == (1, 1, 1), (
        "the pump must convert reader, writer, and buffer once each; found "
        f"{pump_evidence.calls}"
    )
    assert consume_evidence.calls == (1, 1), (
        "consume must convert reader and buffer once each; found "
        f"{consume_evidence.calls}"
    )
    assert pump_evidence.result == len(b"index-like-pump"), (
        "the pump must report the transferred byte count"
    )
    assert consume_evidence.result == b"index-like-consume".decode(), (
        "consume must return the decoded payload"
    )
