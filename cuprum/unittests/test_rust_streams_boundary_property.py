"""Property-based boundary tests for the Rust stream entry points.

The example-based suites in ``test_rust_streams.py`` and
``test_rust_consume_stream.py`` cover a curated set of buffer sizes and I/O
failures. These properties fuzz the argument-validation
boundary of ``rust_pump_stream`` / ``rust_consume_stream`` at the Python/Rust
seam:

- Non-positive and over-cap ``buffer_size`` values raise ``ValueError``
  (validation happens before any descriptor is touched).
- Negative and out-of-``i32``-range descriptors raise ``ValueError``.
- Values beyond ``i64`` stay PyO3 extraction errors.

The accepted arguments — where the default applies and the transfer actually
completes — are covered by ``test_rust_streams_roundtrip_property.py``.

Buffer-size validation runs before descriptor conversion, so the
buffer-size properties can pass a throwaway descriptor without performing
I/O. The descriptor properties use the default (valid) buffer size so that
conversion is the failing step.

The buffer-size properties open a real read end rather than passing ``-1``, so
they hold on every platform: Windows resolves a CRT descriptor to an OS handle
in ``cuprum._streams_rs`` before the native call, and ``msvcrt.get_osfhandle``
raises ``OSError`` for ``-1``, which would mask the buffer rejection the
property asserts. Where a deliberately invalid descriptor is the subject — a
negative or out-of-``int`` value the property supplies itself — the enclosing
property carries ``_buffer_validation_before_descriptor`` to record the same
limit.
"""

from __future__ import annotations

import contextlib
import os
import sys
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

if typ.TYPE_CHECKING:
    from types import ModuleType

# Mirror of MAX_BUFFER_SIZE in rust/cuprum-streams/src/lib.rs (1 GiB).
_MAX_BUFFER_SIZE = 1 << 30
_I32_MAX = (1 << 31) - 1
_I64_MAX = (1 << 63) - 1
_I64_MIN = -(1 << 63)

# The descriptor properties assert the Unix i32 file-descriptor conversion
# contract. On Windows the wrapper routes fds through msvcrt.get_osfhandle and
# the native path accepts pointer-sized handles, so those assertions do not
# hold; skip them there rather than encode platform-specific error semantics.
_unix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts the Unix i32 file-descriptor conversion contract",
)

# Windows resolves a CRT descriptor to a file handle in the Python wrapper
# before Rust receives ``buffer_size``. A ``-1`` throwaway descriptor
# consequently fails there before the buffer-validation boundary can run:
# ``msvcrt.get_osfhandle(-1)`` raises ``OSError(EBADF)``. Properties that hand
# over an open descriptor instead do not need this mark.
_buffer_validation_before_descriptor = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows resolves the descriptor before Rust validates buffer_size",
)

_SUPPRESS_FIXTURE = settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=50,
)


def _safe_close(fd: int) -> None:
    """Close ``fd``, ignoring an already-closed or invalid descriptor."""
    with contextlib.suppress(OSError):
        os.close(fd)


class _BufferSizeEntryPoint(typ.Protocol):
    """An entry point invoked purely to exercise ``buffer_size`` validation."""

    def __call__(self, streams: ModuleType, *, buffer_size: int) -> object:
        """Invoke the entry point with the supplied ``buffer_size``."""
        pass


# Both bounds span the whole signed 64-bit range, so the failure is the
# documented buffer-size rejection rather than an integer-conversion overflow.
# The negative bound reaches ``i64::MIN``: PyO3 extracts ``buffer_size`` as an
# ``i64``, so every value in this range is a well-formed argument that the
# native validator must classify itself rather than reject at extraction.
_OUT_OF_RANGE_BUFFER_SIZES = st.one_of(
    st.integers(min_value=_I64_MIN, max_value=0),
    st.integers(min_value=_MAX_BUFFER_SIZE + 1, max_value=_I64_MAX),
)


class _OpenReaderEntryPoint(typ.Protocol):
    """An entry point invoked with an open descriptor and a buffer size."""

    def __call__(
        self, streams: ModuleType, *, reader_fd: int, buffer_size: int
    ) -> object:
        """Invoke the entry point with the supplied reader and buffer size."""
        ...


def _consume_with_open_reader(
    streams: ModuleType, *, reader_fd: int, buffer_size: int
) -> object:
    """Call ``rust_consume_stream`` with the open reader and buffer size."""
    return streams.rust_consume_stream(reader_fd, buffer_size=buffer_size)


def _pump_with_open_reader(
    streams: ModuleType, *, reader_fd: int, buffer_size: int
) -> object:
    """Call ``rust_pump_stream`` with the open reader and a valid writer.

    The writer is opened separately rather than reusing ``reader_fd``: the
    pump's wrapper closes a writer that never reached the native ownership
    boundary, and closing the reader would leave the descriptor the property
    depends on in an undefined state on the next example.

    Returns
    -------
    object
        Whatever the entry point returns; the property expects it to raise
        instead.
    """
    with contextlib.ExitStack() as stack:
        writer_fd = os.open(os.devnull, os.O_WRONLY)
        stack.callback(_safe_close, writer_fd)
        return streams.rust_pump_stream(reader_fd, writer_fd, buffer_size=buffer_size)


@pytest.mark.parametrize(
    "entry_point",
    [
        pytest.param(_consume_with_open_reader, id="consume"),
        pytest.param(_pump_with_open_reader, id="pump"),
    ],
)
@_SUPPRESS_FIXTURE
@given(bad_size=_OUT_OF_RANGE_BUFFER_SIZES)
def test_rejects_out_of_range_buffer_with_open_reader(
    rust_streams: ModuleType,
    entry_point: _OpenReaderEntryPoint,
    bad_size: int,
) -> None:
    """Both entry points reject an out-of-range buffer for an open reader.

    The reader is never dereferenced: the buffer check runs first and raises.
    An invalid buffer reported as anything but ``ValueError`` — in particular
    as the ``OSError`` a first read from an exhausted descriptor would produce
    — is the regression this catches.

    The read end is real so the property holds on Windows as well as POSIX.
    Handing ``-1`` to the wrapper would fail there during its own descriptor
    preparation, before the native validation this property exists to observe,
    which is why the equivalent property that still uses an invalid descriptor
    carries ``_buffer_validation_before_descriptor``. An open descriptor needs
    no such exclusion, so this is the row that gives the Windows job live
    coverage of the buffer window.
    """
    with contextlib.ExitStack() as stack:
        reader = os.open(os.devnull, os.O_RDONLY)
        stack.callback(_safe_close, reader)
        with pytest.raises(ValueError, match="buffer_size"):
            entry_point(rust_streams, reader_fd=reader, buffer_size=bad_size)


def _consume_with_reader_and_buffer(
    streams: ModuleType, *, reader_fd: int, buffer_size: int
) -> object:
    """Call ``rust_consume_stream`` with the supplied reader and buffer size."""
    return streams.rust_consume_stream(reader_fd, buffer_size=buffer_size)


def _pump_with_reader_and_buffer(
    streams: ModuleType, *, reader_fd: int, buffer_size: int
) -> object:
    """Call ``rust_pump_stream`` with the invalid reader and a valid writer.

    The writer must be a genuinely open descriptor rather than a second copy of
    the invalid value. The wrapper closes a writer that has not yet reached the
    native ownership boundary when pre-native validation fails, and ``os.close``
    raises ``OverflowError`` — not the ``OSError`` that close suppresses — for a
    value outside the C ``int`` range. Reusing these descriptors as the writer
    would therefore surface that error instead of the buffer-size
    ``ValueError`` this property pins.

    Parameters
    ----------
    streams : ModuleType
        The Rust streams module fixture.
    reader_fd : int
        The invalid reader descriptor to pass through.
    buffer_size : int
        The ``buffer_size`` to supply.

    Returns
    -------
    object
        Whatever the entry point returns; the property expects it to raise
        instead.
    """
    with contextlib.ExitStack() as stack:
        writer_fd = os.open(os.devnull, os.O_WRONLY)
        stack.callback(_safe_close, writer_fd)
        return streams.rust_pump_stream(reader_fd, writer_fd, buffer_size=buffer_size)


class _ReaderAndBufferEntryPoint(typ.Protocol):
    """An entry point invoked with both a reader descriptor and a buffer size."""

    def __call__(
        self, streams: ModuleType, *, reader_fd: int, buffer_size: int
    ) -> object:
        """Invoke the entry point with the supplied reader and buffer size."""
        ...


@pytest.mark.parametrize(
    "entry_point",
    [
        pytest.param(_consume_with_reader_and_buffer, id="consume"),
        pytest.param(_pump_with_reader_and_buffer, id="pump"),
    ],
)
@_buffer_validation_before_descriptor
@_SUPPRESS_FIXTURE
@given(
    bad_size=_OUT_OF_RANGE_BUFFER_SIZES,
    bad_fd=st.one_of(
        st.integers(min_value=_I64_MIN, max_value=-1),
        st.integers(min_value=_I32_MAX + 1, max_value=_I64_MAX),
    ),
)
def test_buffer_validation_precedes_descriptor_conversion(
    rust_streams: ModuleType,
    entry_point: _ReaderAndBufferEntryPoint,
    bad_size: int,
    bad_fd: int,
) -> None:
    """An invalid buffer is reported even when the descriptor is also invalid.

    The native order is buffer, reader, writer, adoption. Both arguments are
    invalid here, so the message proves which check ran first: only the buffer
    validator can produce it. Reporting ``file descriptor`` instead would mean
    the order had been reversed, and on the pump path would mean the writer had
    been adopted before the reader was validated — the ownership regression
    this pins. Both values are deliberately interior ``i64``, so PyO3 extracts
    them successfully and the classification is the boundary's own rather than
    an extraction error.
    """
    with pytest.raises(ValueError, match="buffer_size") as excinfo:
        entry_point(rust_streams, reader_fd=bad_fd, buffer_size=bad_size)

    assert "file descriptor" not in str(excinfo.value), (
        "buffer validation must run before descriptor conversion; found "
        f"{str(excinfo.value)!r}"
    )


@_unix_only
@_SUPPRESS_FIXTURE
@given(
    bad_fd=st.one_of(
        st.integers(min_value=_I64_MIN, max_value=-1),
        st.integers(min_value=_I32_MAX + 1, max_value=_I64_MAX),
    ),
)
def test_consume_rejects_invalid_descriptor(
    rust_streams: ModuleType,
    bad_fd: int,
) -> None:
    """Negative or out-of-i32-range descriptors raise ``ValueError``."""
    with pytest.raises(ValueError, match="file descriptor"):
        rust_streams.rust_consume_stream(bad_fd)


@_unix_only
@_SUPPRESS_FIXTURE
@given(
    bad_fd=st.one_of(
        st.integers(min_value=_I64_MIN, max_value=-1),
        st.integers(min_value=_I32_MAX + 1, max_value=_I64_MAX),
    ),
)
def test_pump_rejects_invalid_reader_descriptor(
    rust_streams: ModuleType,
    bad_fd: int,
) -> None:
    """``rust_pump_stream`` rejects an invalid reader descriptor.

    The writer is a genuinely valid descriptor, so the ``ValueError`` can only
    originate from the invalid reader, not from a coincidentally invalid writer.
    """
    with contextlib.ExitStack() as stack:
        writer_fd = os.open(os.devnull, os.O_WRONLY)
        stack.callback(_safe_close, writer_fd)
        with pytest.raises(ValueError, match="file descriptor"):
            rust_streams.rust_pump_stream(bad_fd, writer_fd)


@pytest.mark.parametrize(
    "entry_point",
    [
        pytest.param(_consume_with_open_reader, id="consume"),
        pytest.param(_pump_with_open_reader, id="pump"),
    ],
)
@_SUPPRESS_FIXTURE
@given(
    beyond_i64=st.one_of(
        st.integers(min_value=1 << 63), st.integers(max_value=-((1 << 63) + 1))
    )
)
def test_out_of_i64_buffer_size_stays_an_extraction_error(
    rust_streams: ModuleType,
    entry_point: _OpenReaderEntryPoint,
    beyond_i64: int,
) -> None:
    """Values outside ``i64`` keep PyO3's ``OverflowError``, not ``ValueError``.

    The typed boundary classifies what PyO3 successfully extracted. An
    out-of-range integer never reaches it, so this stays an extraction error:
    reclassifying it as a stream-validation failure would claim the boundary
    saw an argument it never received.

    ``OverflowError`` is a subclass of ``ArithmeticError``, not of
    ``ValueError``, so ``pytest.raises(ValueError)`` would not mask a
    regression here.

    The reader is a real read end for the same reason the window properties use
    one: the argument is never extracted, so the descriptor is not
    dereferenced, but Windows would fail it during the wrapper's own
    preparation and mask the extraction error this property pins.
    """
    with contextlib.ExitStack() as stack:
        reader = os.open(os.devnull, os.O_RDONLY)
        stack.callback(_safe_close, reader)
        with pytest.raises(OverflowError):
            entry_point(rust_streams, reader_fd=reader, buffer_size=beyond_i64)
