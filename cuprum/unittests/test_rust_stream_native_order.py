"""Native-order tests for the compiled Rust stream entry points.

``test_rust_streams_boundary_property.py`` fuzzes the boundary through the
Python shim, ``cuprum._streams_rs``. The shim validates ``buffer_size`` itself
before delegating, so it cannot show what the *compiled* boundary does when a
buffer size and a descriptor are invalid together: whichever check the shim
runs first is the one that answers. These tests call
``cuprum._rust_backend_native`` directly, which is the only way to observe the
ordering the shim is written against.

Two facts are pinned, and both concern resource ownership rather than messages:

- A rejected buffer size is reported before the reader descriptor is converted
  and before the writer is adopted, so the writer is still open afterwards.
- A successful pump does adopt the writer, so the same probe reports it closed.

The second keeps the first honest. A probe that reported "open" for every input
would satisfy the first assertion without having observed anything, so the
contrast is what makes "open" evidence of a check that ran early.

The same reasoning separates the extraction errors below from the validation
errors above. Wrong Python types and integers outside ``i64`` fail argument
extraction rather than stream validation, so they keep ``PyO3``'s ``TypeError``
and ``OverflowError`` and are deliberately not reclassified as stream-domain
failures. The shim raises its own ``OverflowError`` for that range, with the
message ``PyO3`` uses, so only a direct native call shows that ``PyO3`` itself
still behaves this way.

PyO3 documents that the generated wrapper extracts regular and keyword
arguments, but it does not specify an order across several arguments
(https://pyo3.rs/main/function). Each extraction case below therefore gets its
own call, and no test depends on which of two invalid arguments is reported
first. That is also why the wrappers validate in Python before delegating: the
documented precedence cannot be delegated to extraction.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import os
import platform
import sys
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum import _rust_backend
from cuprum._constants import DEFAULT_NATIVE_BUFFER_SIZE
from cuprum.unittests._rust_stream_test_support import (
    _descriptor_state,
    _open_pipe_sink,
    _OwnedDescriptor,
    _safe_close,
)

if typ.TYPE_CHECKING:
    from types import ModuleType

_I64_MIN = -(1 << 63)
_MAX_BUFFER_SIZE = 1 << 30

# The pump retains the Unix i32 descriptor contract. Windows consume now uses
# a CRT descriptor, so its native validation-order cases run on both platforms.
_unix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts the Unix descriptor contract the shim converts on Windows",
)
_cpython_only = pytest.mark.skipif(
    platform.python_implementation() != "CPython",
    reason="index conversion counts are a CPython implementation detail",
)
_native_property = settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=50,
)


class _IndexOnly:
    """Expose an integer through ``__index__`` and count conversions."""

    def __init__(self, value: int) -> None:
        """Wrap ``value`` with a fresh conversion count."""
        self.value = value
        self.calls = 0

    def __index__(self) -> int:
        """Return the wrapped integer, recording the conversion."""
        self.calls += 1
        return self.value


@pytest.fixture(name="native_streams")
def fixture_native_streams() -> ModuleType:
    """Provide the compiled native module, skipping when it is absent.

    Returns
    -------
    ModuleType
        The imported ``cuprum._rust_backend_native`` module.
    """
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed.")
    return importlib.import_module("cuprum._rust_backend_native")


@_unix_only
def test_pump_reports_a_rejected_buffer_before_touching_the_writer(
    native_streams: ModuleType,
) -> None:
    """A buffer failure precedes reader conversion and writer adoption.

    The reader is ``-1``, which the converter would also reject, so the message
    proves which check ran first: only the buffer validator produces it. A
    reversed order would report ``file descriptor`` instead, and the writer
    would have been adopted — which the state probe rules out.
    """
    with contextlib.ExitStack() as stack:
        writer = _open_pipe_sink(stack)
        with pytest.raises(ValueError, match="buffer_size") as excinfo:
            native_streams.rust_pump_stream(-1, writer.fd, buffer_size=0)

        assert "file descriptor" not in str(excinfo.value), (
            "buffer validation must precede descriptor conversion; found "
            f"{str(excinfo.value)!r}"
        )
        assert _descriptor_state(writer.fd) == "open", (
            "a rejected buffer size must not adopt the writer"
        )


@_unix_only
def test_pump_adopts_and_closes_the_writer_on_success(
    native_streams: ModuleType,
) -> None:
    """A completed pump leaves the adopted writer closed.

    This is the contrast that makes the preceding test's ``"open"`` result
    meaningful: the identical probe reports ``"closed"`` once the ownership
    boundary has been crossed.

    Raises
    ------
    OSError
        If the pump fails its I/O after adoption. The ownership guard catches
        it to disarm cleanup and re-raises, so the failure is never hidden.
    """
    payload = b"native-order-payload"
    with contextlib.ExitStack() as stack:
        reader_fd, upstream_fd = os.pipe()
        stack.callback(_safe_close, reader_fd)
        upstream = _OwnedDescriptor(upstream_fd)
        stack.callback(upstream.close)
        writer = _open_pipe_sink(stack)
        os.write(upstream.fd, payload)
        # Close the source writer so the pump observes EOF. The owner is
        # disarmed with the close, so the number cannot be closed twice.
        upstream.close()

        try:
            moved = native_streams.rust_pump_stream(
                reader_fd, writer.fd, buffer_size=1024
            )
        except OSError:
            # A native I/O failure happens after adoption, so the pump has
            # already closed the writer; disarm rather than close a number
            # Rust released.
            writer.disarm()
            raise
        # The pump returned, so it adopted and closed the writer.
        writer.disarm()

        assert moved == len(payload), "the pump must transfer every payload byte"
        assert _descriptor_state(writer.fd) == "closed", (
            "a completed pump must have adopted the writer"
        )


@_unix_only
def test_owner_closes_only_while_the_contract_leaves_it_armed(
    native_streams: ModuleType,
) -> None:
    """Cleanup follows the call's ownership contract, never descriptor state.

    A pump rejected before adoption leaves the writer with the test, so the
    armed owner closes it once. A pump that adopted the writer leaves the owner
    disarmed; if that number is later reused, the owner must not close it.
    Both directions are decided by which call was made, not by inspecting
    descriptor metadata.

    The direct native pump takes an owned Win32 handle on Windows and adopts
    it before validating the buffer, so its CRT descriptors are not valid
    inputs. The Windows adoption contract is pinned in
    ``test_rust_errno_windows.py`` instead.
    """
    with contextlib.ExitStack() as stack:
        reader_fd, writer_fd = os.pipe()
        stack.callback(_safe_close, reader_fd)
        upstream = _OwnedDescriptor(writer_fd)
        stack.callback(upstream.close)
        writer = _open_pipe_sink(stack)
        with pytest.raises(ValueError, match="buffer_size"):
            native_streams.rust_pump_stream(reader_fd, writer.fd, buffer_size=0)

        assert _descriptor_state(writer.fd) == "open", (
            "a rejected buffer must not adopt the writer"
        )
        writer.close()
        assert _descriptor_state(writer.fd) == "closed", (
            "the owner must still close a writer the native side never took"
        )

    # A disarmed owner is inert: let another open reclaim the number and check
    # that the owner's close leaves it alone.
    with contextlib.ExitStack() as stack:
        adopted = _open_pipe_sink(stack)
        released_number = adopted.fd
        adopted.disarm()
        _safe_close(released_number)
        reclaimed = os.open(os.devnull, os.O_WRONLY)
        stack.callback(_safe_close, reclaimed)
        assert reclaimed == released_number, (
            "the scenario needs the released number to be reclaimed"
        )
        adopted.close()
        assert _descriptor_state(reclaimed) == "open", (
            "a disarmed owner must not close a number another owner has reused"
        )


@_unix_only
def test_native_pump_io_failure_after_adoption_releases_the_writer(
    native_streams: ModuleType,
) -> None:
    """A read that fails after adoption still leaves the adopted writer closed.

    The reader is the write end of a pipe, so descriptor conversion succeeds
    and the native entry adopts the writer before the read fails. This is the
    post-adoption failure the ownership probe alone cannot classify.
    """
    with contextlib.ExitStack() as stack:
        read_end, write_end = os.pipe()
        stack.callback(_safe_close, read_end)
        # A pipe's write end opens and converts like any descriptor, but a
        # read from it fails with EBADF once the pump has adopted the writer.
        reader = _OwnedDescriptor(write_end)
        stack.callback(reader.close)
        sink = _open_pipe_sink(stack)
        with pytest.raises(OSError, match=r"\[Errno 9\]|\[WinError (6|87)\]"):
            native_streams.rust_pump_stream(reader.fd, sink.fd, buffer_size=1024)

        # The pump failed inside native I/O, which is only reachable after the
        # writer was adopted and closed there.
        sink.disarm()

        assert _descriptor_state(reader.fd) == "open", (
            "a failed read must leave the borrowed reader open"
        )
        assert _descriptor_state(sink.fd) == "closed", (
            "the adopted writer must be closed even when the pump fails"
        )


def test_consume_reports_a_rejected_buffer_before_reading_the_reader(
    native_streams: ModuleType,
) -> None:
    """``rust_consume_stream`` validates the buffer before the reader too.

    The consume path has no writer to adopt, so the message is the whole
    witness: an invalid buffer size reported while the reader is equally
    invalid can only come from the buffer validator.
    """
    with pytest.raises(ValueError, match="buffer_size") as excinfo:
        native_streams.rust_consume_stream(-1, buffer_size=0)

    assert "file descriptor" not in str(excinfo.value), (
        "buffer validation must precede descriptor conversion; found "
        f"{str(excinfo.value)!r}"
    )


def test_consume_buffer_error_leaves_borrowed_reader_usable(
    native_streams: ModuleType,
) -> None:
    """A rejected buffer neither closes nor reads the caller's valid reader."""
    payload = b"still available after buffer rejection"
    with contextlib.ExitStack() as stack:
        reader_fd, writer_fd = os.pipe()
        stack.callback(_safe_close, reader_fd)
        upstream = _OwnedDescriptor(writer_fd)
        stack.callback(upstream.close)
        os.write(upstream.fd, payload)
        upstream.close()

        with pytest.raises(ValueError, match="buffer_size"):
            native_streams.rust_consume_stream(reader_fd, buffer_size=0)

        assert _descriptor_state(reader_fd) == "open", (
            "consume borrows the reader on validation failure"
        )
        assert os.read(reader_fd, len(payload)) == payload, (
            "buffer validation must happen before the reader is consumed"
        )


@pytest.mark.parametrize(
    "wrong",
    [
        pytest.param("not-an-integer", id="str"),
        pytest.param(None, id="none"),
        pytest.param(1.5, id="float"),
    ],
)
def test_wrong_argument_types_keep_pyo3_extraction_errors(
    native_streams: ModuleType,
    wrong: object,
) -> None:
    """Wrong Python types stay ``TypeError``, not stream validation errors.

    ``TypeError`` and ``ValueError`` are disjoint, so ``pytest.raises`` here
    would not mask a regression that routed an extraction failure through the
    typed boundary.
    """
    with contextlib.ExitStack() as stack:
        reader_fd, reader_writer_fd = os.pipe()
        stack.callback(_safe_close, reader_fd)
        stack.callback(_safe_close, reader_writer_fd)
        writer = _open_pipe_sink(stack)
        with pytest.raises(TypeError):
            native_streams.rust_consume_stream(wrong)

        with pytest.raises(TypeError):
            native_streams.rust_pump_stream(wrong, writer.fd)

        with pytest.raises(TypeError):
            native_streams.rust_pump_stream(reader_fd, wrong)

        with pytest.raises(TypeError):
            native_streams.rust_consume_stream(reader_fd, buffer_size=wrong)

        with pytest.raises(TypeError):
            native_streams.rust_pump_stream(reader_fd, writer.fd, buffer_size=wrong)

        assert _descriptor_state(reader_fd) == "open", (
            "PyO3 extraction failure must leave the borrowed reader open"
        )
        assert _descriptor_state(writer.fd) == "open", (
            "PyO3 extraction failure must happen before writer adoption"
        )


@pytest.mark.parametrize(
    "beyond_i64",
    [
        pytest.param(1 << 63, id="above_i64_max"),
        pytest.param(-((1 << 63) + 1), id="below_i64_min"),
    ],
)
def test_out_of_i64_buffer_keeps_pyo3_overflow_error(
    native_streams: ModuleType,
    beyond_i64: int,
) -> None:
    """Integers outside ``i64`` stay ``OverflowError`` at the native boundary.

    The shim raises its own ``OverflowError`` for this range before the native
    call, using the message ``PyO3`` uses, so the shim's behaviour alone cannot
    show that ``PyO3`` still does this. Only a direct call separates the two.
    """
    with contextlib.ExitStack() as stack:
        reader_fd = os.open(os.devnull, os.O_RDONLY)
        stack.callback(_safe_close, reader_fd)
        writer = _open_pipe_sink(stack)
        with pytest.raises(OverflowError):
            native_streams.rust_consume_stream(reader_fd, buffer_size=beyond_i64)
        with pytest.raises(OverflowError):
            native_streams.rust_pump_stream(
                reader_fd,
                writer.fd,
                buffer_size=beyond_i64,
            )

        assert _descriptor_state(reader_fd) == "open", (
            "PyO3 extraction failure must leave the borrowed reader open"
        )
        assert _descriptor_state(writer.fd) == "open", (
            "PyO3 extraction failure must happen before writer adoption"
        )


@pytest.mark.parametrize(
    "beyond_i64",
    [
        pytest.param(1 << 63, id="above_i64_max"),
        pytest.param(-((1 << 63) + 1), id="below_i64_min"),
    ],
)
def test_out_of_i64_descriptors_keep_pyo3_overflow_error(
    native_streams: ModuleType,
    beyond_i64: int,
) -> None:
    """Descriptor extraction outside ``i64`` stays ``OverflowError``."""
    with contextlib.ExitStack() as stack:
        writer = _open_pipe_sink(stack)
        with pytest.raises(OverflowError):
            native_streams.rust_consume_stream(beyond_i64)

        with pytest.raises(OverflowError):
            native_streams.rust_pump_stream(beyond_i64, writer.fd)

        assert _descriptor_state(writer.fd) == "open", (
            "descriptor extraction failure must happen before writer adoption"
        )


# A valid buffer size is what lets descriptor conversion be the step that
# fails. Every other invalid-descriptor test here pairs the descriptor with an
# invalid buffer, so the buffer validator answers first and the
# `InvalidDescriptor` arm of the conversion is never reached.
_VALID_BUFFER_SIZE = 65536


def test_consume_maps_an_invalid_descriptor_to_value_error(
    native_streams: ModuleType,
) -> None:
    """``InvalidDescriptor`` reaches Python as ``ValueError``, not ``OSError``.

    The buffer size is valid and the descriptor is not, so validation passes
    the first check and the reader is the step that fails. That is the only
    way to exercise ``RustStreamError::InvalidDescriptor`` through
    ``From<RustStreamError> for PyErr``: with an invalid buffer the earlier
    validator exits before the converter runs, and a native unit test can pin
    the Rust variant without ever showing which exception a caller sees.

    The two classes are disjoint, so ``pytest.raises(ValueError)`` would not
    mask a regression that routed this arm through the ``OSError`` mapping.
    """
    with pytest.raises(ValueError, match="file descriptor"):
        native_streams.rust_consume_stream(-1, buffer_size=_VALID_BUFFER_SIZE)


@_unix_only
def test_pump_maps_an_invalid_reader_descriptor_to_value_error(
    native_streams: ModuleType,
) -> None:
    """``rust_pump_stream`` maps an invalid reader to ``ValueError`` too.

    The writer is a genuinely open descriptor, so the failure can only come
    from the reader, and the valid buffer size keeps the buffer validator out
    of the way. The pump has its own conversion path through
    ``run_stream_operation``, so asserting only the consume path would leave
    the pump export's mapping unpinned.
    """
    with contextlib.ExitStack() as stack:
        writer = _open_pipe_sink(stack)
        with pytest.raises(ValueError, match="file descriptor"):
            native_streams.rust_pump_stream(
                -1, writer.fd, buffer_size=_VALID_BUFFER_SIZE
            )

        assert _descriptor_state(writer.fd) == "open", (
            "an invalid reader stops the call before the writer is adopted"
        )


def _default_buffer_size(native: ModuleType, name: str) -> int | None:
    """Return the native ``buffer_size`` default, or ``None`` if hidden."""
    function = getattr(native, name)
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return None
    parameter = signature.parameters.get("buffer_size")
    if parameter is None or parameter.default is inspect.Parameter.empty:
        return None
    return typ.cast("int", parameter.default)


@pytest.mark.parametrize("name", ["rust_pump_stream", "rust_consume_stream"])
def test_native_declares_the_shared_default_buffer(
    native_streams: ModuleType,
    name: str,
) -> None:
    """Both exports declare the domain default the Python wrappers use.

    PyO3 publishes the ``#[pyo3(signature = ...)]`` default through
    ``__text_signature__``. When introspection is unavailable the behavioural
    default tests below still pin that omitting the argument works.
    """
    default = _default_buffer_size(native_streams, name)
    if default is None:
        pytest.skip(f"{name} exposes no introspectable buffer_size default")

    assert default == DEFAULT_NATIVE_BUFFER_SIZE, (
        f"{name} must default to the shared 64 KiB buffer, found {default}"
    )


def test_native_consume_reads_with_the_declared_default_buffer(
    native_streams: ModuleType,
) -> None:
    """Omitting ``buffer_size`` consumes the whole stream at the native default."""
    payload = b"native-default-consume"
    with contextlib.ExitStack() as stack:
        reader_fd, writer_fd = os.pipe()
        stack.callback(_safe_close, reader_fd)
        upstream = _OwnedDescriptor(writer_fd)
        stack.callback(upstream.close)
        os.write(upstream.fd, payload)
        upstream.close()

        assert native_streams.rust_consume_stream(reader_fd) == payload.decode(), (
            "the native default buffer must consume the whole stream"
        )


@_unix_only
def test_native_pump_transfers_with_the_declared_default_buffer(
    native_streams: ModuleType,
) -> None:
    """Omitting ``buffer_size`` pumps the whole stream at the native default."""
    payload = b"native-default-pump"
    with contextlib.ExitStack() as stack:
        reader_fd, writer_fd = os.pipe()
        stack.callback(_safe_close, reader_fd)
        upstream = _OwnedDescriptor(writer_fd)
        stack.callback(upstream.close)
        sink = _open_pipe_sink(stack)
        os.write(upstream.fd, payload)
        upstream.close()

        try:
            moved = native_streams.rust_pump_stream(reader_fd, sink.fd)
        finally:
            # The native entry adopts and closes the writer on return or on a
            # post-adoption I/O failure, so the test never closes it again.
            sink.disarm()

        assert moved == len(payload), (
            "the native default buffer must transfer the whole stream"
        )


@_unix_only
@_cpython_only
def test_native_extracts_index_like_arguments_once(
    native_streams: ModuleType,
) -> None:
    """``__index__`` inputs are accepted and converted exactly once each.

    The writer is index-like as well, so this also shows that the pump's
    ownership boundary accepts the same protocol as its read side.
    """
    payload = b"index-like-arguments"
    with contextlib.ExitStack() as stack:
        reader_fd, writer_fd = os.pipe()
        stack.callback(_safe_close, reader_fd)
        upstream = _OwnedDescriptor(writer_fd)
        stack.callback(upstream.close)
        sink = _open_pipe_sink(stack)
        os.write(upstream.fd, payload)
        upstream.close()

        reader = _IndexOnly(reader_fd)
        sink_argument = _IndexOnly(sink.fd)
        size = _IndexOnly(1024)
        try:
            moved = native_streams.rust_pump_stream(
                reader, sink_argument, buffer_size=size
            )
        finally:
            # Conversion and transfer succeeded, so the native entry owns the
            # writer from here on.
            sink.disarm()

        assert moved == len(payload), "the index-like writer must receive every byte"
        assert _descriptor_state(sink.fd) == "closed", (
            "the extracted writer must have been adopted by the native pump"
        )
        assert (reader.calls, sink_argument.calls, size.calls) == (1, 1, 1), (
            "each index-like argument must be converted exactly once; found "
            f"reader={reader.calls}, writer={sink_argument.calls}, "
            f"buffer={size.calls}"
        )


@_native_property
@given(
    bad_size=st.one_of(
        st.integers(min_value=_I64_MIN, max_value=0),
        st.integers(min_value=_MAX_BUFFER_SIZE + 1, max_value=(1 << 63) - 1),
    )
)
def test_native_consume_rejects_a_buffer_without_consuming_the_reader(
    native_streams: ModuleType,
    bad_size: int,
) -> None:
    """The direct native boundary rejects the buffer before reading anything.

    Both arguments are otherwise valid: the reader is open and holds a
    complete payload, and the buffer is the only invalid value. That separates
    this property from the wrapper tests that pair two failures and can only
    observe precedence.
    """
    payload = b"left unread by rejected buffer"
    with contextlib.ExitStack() as stack:
        reader_fd, writer_fd = os.pipe()
        stack.callback(_safe_close, reader_fd)
        upstream = _OwnedDescriptor(writer_fd)
        stack.callback(upstream.close)
        os.write(upstream.fd, payload)
        upstream.close()

        with pytest.raises(ValueError, match="buffer_size"):
            native_streams.rust_consume_stream(reader_fd, buffer_size=bad_size)

        assert os.read(reader_fd, len(payload)) == payload, (
            "the native boundary must not read the stream before buffer validation"
        )
