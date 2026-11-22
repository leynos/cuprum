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

from cuprum.unittests._rust_stream_test_support import (
    _descriptor_state,
    _OwnedDescriptor,
    _safe_close,
)
from tests.behaviour._stream_adapter_contract_support import (
    _call_with_descriptor,
    _entry_point,
    _ExtractionEvidence,
    _feed_pipe,
    _IndexEvidence,
    _IndexOnly,
    _open_writer,
    _PlatformEvidence,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType


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
    "Non-index-like stream arguments raise TypeError",
)
def test_non_index_like_arguments_raise_type_error() -> None:
    """An argument that refuses the index protocol fails before native I/O."""


@scenario(
    "../features/rust_streams.feature",
    "A Windows writer transfer failure raises OSError",
)
def test_windows_writer_transfer_failure() -> None:
    """A closed writer is rejected by the Windows transfer step."""


@scenario(
    "../features/rust_streams.feature",
    "A Windows reader setup failure raises OSError",
)
def test_windows_reader_setup_failure() -> None:
    """A closed reader is rejected by the Windows conversion step."""


@scenario(
    "../features/rust_streams.feature",
    "Index-like stream arguments are accepted by the Rust pump",
)
def test_pump_accepts_index_like_arguments() -> None:
    """The pump converts each index-like argument exactly once."""


@scenario(
    "../features/rust_streams.feature",
    "Index-like stream arguments are accepted by consume",
)
def test_consume_accepts_index_like_arguments() -> None:
    """Consume converts each index-like argument exactly once."""


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


@pytest.fixture(name="owned_descriptors")
def fixture_owned_descriptors() -> cabc.Iterator[list[_OwnedDescriptor]]:
    """Close every descriptor a scenario opens, once each, after it ends."""
    owners: list[_OwnedDescriptor] = []
    yield owners
    for owner in reversed(owners):
        owner.close()


@given("the Windows platform adapter is in use")
def given_windows_platform() -> None:
    """Require the Windows CRT-handle platform step for the scenario."""
    if os.name != "nt":
        pytest.skip("the platform step exists only on Windows")


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
        upstream = _OwnedDescriptor(upstream_fd)
        stack.callback(upstream.close)
        sink = _open_writer()
        stack.callback(sink.close)
        _feed_pipe(reader_fd, upstream, payload)

        reader = _IndexOnly(reader_fd)
        sink_argument = _IndexOnly(sink.fd)
        size = _IndexOnly(1024)
        try:
            moved = adapters.rust_pump_stream(
                typ.cast("int", reader),
                typ.cast("int", sink_argument),
                buffer_size=typ.cast("int", size),
            )
        finally:
            sink.disarm_if_adopted()

    assert moved == len(payload), f"the pump must transfer every byte, found {moved!r}"
    return _IndexEvidence(moved, (reader.calls, sink_argument.calls, size.calls))


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
        upstream = _OwnedDescriptor(upstream_fd)
        stack.callback(upstream.close)
        _feed_pipe(reader_fd, upstream, payload)

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


@then("the pump returns its payload and converted every argument once")
def then_pump_index_like(pump_evidence: _IndexEvidence) -> None:
    """Assert the pump converted each index-like argument exactly once."""
    assert pump_evidence.calls == (1, 1, 1), (
        "the pump must convert reader, writer, and buffer once each; found "
        f"{pump_evidence.calls}"
    )
    assert pump_evidence.result == len(b"index-like-pump"), (
        "the pump must report the transferred byte count"
    )


@then("consume returns its payload and converted every argument once")
def then_consume_index_like(consume_evidence: _IndexEvidence) -> None:
    """Assert consume converted each index-like argument exactly once."""
    assert consume_evidence.calls == (1, 1), (
        "consume must convert reader and buffer once each; found "
        f"{consume_evidence.calls}"
    )
    assert consume_evidence.result == b"index-like-consume".decode(), (
        "consume must return the decoded payload"
    )


@when(
    parsers.parse("the {operation} entry point receives a non-index-like {argument}"),
    target_fixture="extraction_evidence",
)
def when_non_index_like(
    adapters: ModuleType,
    owned_descriptors: list[_OwnedDescriptor],
    operation: str,
    argument: str,
) -> _ExtractionEvidence:
    """Call one adapter with a value that refuses the index protocol."""
    payload = b"unread after an extraction failure"
    reader_fd, upstream_fd = os.pipe()
    reader = _OwnedDescriptor(reader_fd)
    upstream = _OwnedDescriptor(upstream_fd)
    owned_descriptors.extend([reader, upstream])
    _feed_pipe(reader_fd, upstream, payload)

    writer: _OwnedDescriptor | None = None
    if operation == "pump":
        writer = _open_writer()
        owned_descriptors.append(writer)
    not_index = object()
    reader_argument: object = not_index if argument == "reader" else reader_fd
    buffer_argument: object = not_index if argument == "buffer" else 1024
    args: tuple[object, ...] = (
        (reader_argument, writer.fd) if writer is not None else (reader_argument,)
    )
    try:
        with pytest.raises(BaseException) as excinfo:  # ruff: ignore[pytest-raises-too-broad] - the Then step asserts the class.
            _entry_point(adapters, operation)(*args, buffer_size=buffer_argument)
    finally:
        if writer is not None:
            # Every pre-native failure closes the writer the wrapper owns.
            writer.disarm_if_adopted()
    return _ExtractionEvidence(
        excinfo.value,
        reader_fd,
        payload,
        writer.fd if writer is not None else None,
    )


@then("it raises TypeError leaving the caller's reader unread")
def then_type_error(extraction_evidence: _ExtractionEvidence) -> None:
    """Assert a refused argument fails before native I/O or writer adoption."""
    assert isinstance(extraction_evidence.error, TypeError), (
        "expected TypeError, found "
        f"{type(extraction_evidence.error).__name__}: {extraction_evidence.error}"
    )
    assert (
        os.read(extraction_evidence.reader_fd, len(extraction_evidence.payload))
        == extraction_evidence.payload
    ), "the reader must hold its whole payload after an extraction failure"
    if extraction_evidence.writer_fd is not None:
        assert _descriptor_state(extraction_evidence.writer_fd) == "closed", (
            "the wrapper must close a writer that never reached native ownership"
        )


@when(
    "the pump receives a closed writer descriptor", target_fixture="platform_evidence"
)
def when_pump_closed_writer(
    adapters: ModuleType,
    owned_descriptors: list[_OwnedDescriptor],
) -> _PlatformEvidence:
    """Pump into a descriptor number that was closed before the call."""
    payload = b"still in the pipe"
    reader_fd, upstream_fd = os.pipe()
    reader = _OwnedDescriptor(reader_fd)
    upstream = _OwnedDescriptor(upstream_fd)
    owned_descriptors.extend([reader, upstream])
    _feed_pipe(reader_fd, upstream, payload)

    closed_writer = _open_writer()
    closed_writer.close()
    with pytest.raises(BaseException) as excinfo:  # ruff: ignore[pytest-raises-too-broad] - the Then step asserts the class.
        adapters.rust_pump_stream(reader_fd, closed_writer.fd, buffer_size=1024)
    return _PlatformEvidence(excinfo.value, reader_fd, payload)


@when("consume receives a closed reader descriptor", target_fixture="platform_error")
def when_consume_closed_reader(adapters: ModuleType) -> BaseException:
    """Consume from a descriptor number that was closed before the call."""
    closed_reader = _OwnedDescriptor(os.open(os.devnull, os.O_RDONLY))
    closed_reader.close()
    with pytest.raises(BaseException) as excinfo:  # ruff: ignore[pytest-raises-too-broad] - the Then step asserts the class.
        adapters.rust_consume_stream(closed_reader.fd, buffer_size=1024)
    return excinfo.value


@then("it raises OSError and the payload remains unread")
def then_transfer_failure(platform_evidence: _PlatformEvidence) -> None:
    """Assert a failed writer transfer leaves the caller's reader untouched."""
    assert isinstance(platform_evidence.error, OSError), (
        "expected OSError, found "
        f"{type(platform_evidence.error).__name__}: {platform_evidence.error}"
    )
    assert (
        os.read(platform_evidence.reader_fd, len(platform_evidence.payload))
        == platform_evidence.payload
    ), "a failed writer transfer must not consume the reader"


@then("it raises OSError")
def then_os_error(platform_error: BaseException) -> None:
    """Assert the platform step reports its failure as ``OSError``."""
    assert isinstance(platform_error, OSError), (
        f"expected OSError, found {type(platform_error).__name__}: {platform_error}"
    )
