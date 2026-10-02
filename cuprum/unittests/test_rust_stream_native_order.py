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
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
import typing as typ

import pytest

from cuprum import _rust_backend
from cuprum.unittests._rust_stream_test_support import _safe_close

if typ.TYPE_CHECKING:
    from types import ModuleType

#: ``PyO3`` extracts every argument before the function body runs, so these
#: values never reach the converter. The writer is ``-1`` rather than a real
#: descriptor so that a regression which *did* reach the body could not adopt
#: anything belonging to the test process.
_UNUSED_WRITER_FD = -1

# The native module takes the Unix i32 descriptor contract. On Windows the shim
# converts a CRT descriptor to an OS handle and duplicates it before the native
# call, so a raw descriptor cannot be handed over there; these orderings are
# asserted on the platform whose contract they describe.
_unix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts the Unix descriptor contract the shim converts on Windows",
)


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


def _descriptor_state(fd: int) -> str:
    """Return ``"open"`` or ``"closed"`` for a descriptor.

    ``os.fstat`` is the cheapest probe that distinguishes the two without
    disturbing the descriptor, which is what lets a test assert whether a
    writer was adopted.

    Parameters
    ----------
    fd : int
        The descriptor to probe.

    Returns
    -------
    str
        ``"open"`` when the descriptor is still usable, ``"closed"`` when it
        is not.
    """
    try:
        os.fstat(fd)
    except OSError:
        return "closed"
    return "open"


def _open_writer() -> int:
    """Open a writable descriptor for the pump to adopt."""
    return os.open(os.devnull, os.O_WRONLY)


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
        writer_fd = _open_writer()
        stack.callback(_safe_close, writer_fd)

        with pytest.raises(ValueError, match="buffer_size") as excinfo:
            native_streams.rust_pump_stream(-1, writer_fd, buffer_size=0)

        assert "file descriptor" not in str(excinfo.value), (
            "buffer validation must precede descriptor conversion; found "
            f"{str(excinfo.value)!r}"
        )
        assert _descriptor_state(writer_fd) == "open", (
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
    """
    payload = b"native-order-payload"
    with contextlib.ExitStack() as stack:
        writer_fd = _open_writer()
        stack.callback(_safe_close, writer_fd)
        reader_fd, upstream_fd = os.pipe()
        stack.callback(_safe_close, reader_fd)
        stack.callback(_safe_close, upstream_fd)

        os.write(upstream_fd, payload)
        # Close the source writer so the pump observes EOF; the ExitStack's
        # second close of the same descriptor is a harmless no-op.
        _safe_close(upstream_fd)

        moved = native_streams.rust_pump_stream(reader_fd, writer_fd, buffer_size=1024)

        assert moved == len(payload), "the pump must transfer every payload byte"
        assert _descriptor_state(writer_fd) == "closed", (
            "a completed pump must have adopted the writer"
        )


@_unix_only
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
    with pytest.raises(TypeError):
        native_streams.rust_consume_stream(wrong)

    with pytest.raises(TypeError):
        native_streams.rust_pump_stream(wrong, _UNUSED_WRITER_FD)


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
    with pytest.raises(OverflowError):
        native_streams.rust_consume_stream(-1, buffer_size=beyond_i64)


# A valid buffer size is what lets descriptor conversion be the step that
# fails. Every other invalid-descriptor test here pairs the descriptor with an
# invalid buffer, so the buffer validator answers first and the
# `InvalidDescriptor` arm of the conversion is never reached.
_VALID_BUFFER_SIZE = 65536


@_unix_only
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
        writer_fd = _open_writer()
        stack.callback(_safe_close, writer_fd)

        with pytest.raises(ValueError, match="file descriptor"):
            native_streams.rust_pump_stream(
                -1, writer_fd, buffer_size=_VALID_BUFFER_SIZE
            )
