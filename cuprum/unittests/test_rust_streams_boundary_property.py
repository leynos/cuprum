"""Property-based boundary tests for the Rust stream entry points.

The example-based suites in ``test_rust_streams.py`` and
``test_rust_consume_stream.py`` cover a curated set of buffer sizes and I/O
failures. These properties fuzz the argument-validation
boundary of ``rust_pump_stream`` / ``rust_consume_stream`` at the Python/Rust
seam:

- Non-positive and over-cap ``buffer_size`` values raise ``ValueError``
  (validation happens before any descriptor is touched).
- Negative and out-of-``i32``-range descriptors raise ``ValueError``.
- Omitting ``buffer_size`` is equivalent to passing the explicit default.

Buffer-size validation runs before descriptor conversion, so the
buffer-size properties can pass a throwaway descriptor without performing
I/O. The descriptor properties use the default (valid) buffer size so that
conversion is the failing step.
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

# Mirror of MAX_BUFFER_SIZE in rust/cuprum-rust/src/lib.rs (1 GiB).
_MAX_BUFFER_SIZE = 1 << 30
_DEFAULT_BUFFER_SIZE = 65536
_I32_MAX = (1 << 31) - 1
_I64_MAX = (1 << 63) - 1
# A descriptor value that is deterministically invalid, used only where
# validation fails before the descriptor is dereferenced. -1 never names an
# open descriptor, so a validation-order regression fails loudly instead of
# blocking on a real fd such as stdin (0).
_UNUSED_FD = -1

# The descriptor properties assert the Unix i32 file-descriptor conversion
# contract. On Windows the wrapper routes fds through msvcrt.get_osfhandle and
# the native path accepts pointer-sized handles, so those assertions do not
# hold; skip them there rather than encode platform-specific error semantics.
_unix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts the Unix i32 file-descriptor conversion contract",
)

_SUPPRESS_FIXTURE = settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=50,
)


def _safe_close(fd: int) -> None:
    """Close ``fd``, ignoring an already-closed or invalid descriptor.

    Defined locally so the packaged test module stays importable from an
    installed wheel, which ships ``cuprum/unittests`` but not ``tests``.
    """
    with contextlib.suppress(OSError):
        os.close(fd)


class _BufferSizeEntryPoint(typ.Protocol):
    """An entry point invoked purely to exercise ``buffer_size`` validation."""

    def __call__(self, streams: ModuleType, *, buffer_size: int) -> object:
        """Invoke the entry point with the supplied ``buffer_size``."""
        ...


def _consume_with_buffer_size(streams: ModuleType, *, buffer_size: int) -> object:
    """Call ``rust_consume_stream`` with the supplied ``buffer_size``."""
    return streams.rust_consume_stream(_UNUSED_FD, buffer_size=buffer_size)


def _pump_with_buffer_size(streams: ModuleType, *, buffer_size: int) -> object:
    """Call ``rust_pump_stream`` with the supplied ``buffer_size``."""
    return streams.rust_pump_stream(_UNUSED_FD, _UNUSED_FD, buffer_size=buffer_size)


# Both bounds stay inside ``i64`` so the failure is the documented buffer-size
# rejection rather than an integer-conversion overflow.
_OUT_OF_RANGE_BUFFER_SIZES = st.one_of(
    st.integers(min_value=-(1 << 62), max_value=0),
    st.integers(min_value=_MAX_BUFFER_SIZE + 1, max_value=_I64_MAX),
)


@pytest.mark.parametrize(
    "entry_point",
    [
        pytest.param(_consume_with_buffer_size, id="consume"),
        pytest.param(_pump_with_buffer_size, id="pump"),
    ],
)
@_SUPPRESS_FIXTURE
@given(bad_size=_OUT_OF_RANGE_BUFFER_SIZES)
def test_rejects_out_of_range_buffer(
    rust_streams: ModuleType,
    entry_point: _BufferSizeEntryPoint,
    bad_size: int,
) -> None:
    """Both entry points reject any ``buffer_size`` outside ``1..=1 GiB``.

    Validation precedes descriptor conversion, so a throwaway descriptor is
    enough and no I/O is performed.
    """
    with pytest.raises(ValueError, match="buffer_size"):
        entry_point(rust_streams, buffer_size=bad_size)


@_unix_only
@_SUPPRESS_FIXTURE
@given(
    bad_fd=st.one_of(
        st.integers(min_value=-(1 << 62), max_value=-1),
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
        st.integers(min_value=-(1 << 62), max_value=-1),
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


def _feed_pipe(write_fd: int, payload: bytes) -> None:
    """Write ``payload`` fully into ``write_fd``."""
    view = memoryview(payload)
    while view:
        written = os.write(write_fd, view)
        view = view[written:]


def _consume_via_pipe(
    rust_streams: ModuleType,
    payload: bytes,
    **kwargs: object,
) -> str:
    """Write ``payload`` through a pipe and consume it with the Rust decoder."""
    with contextlib.ExitStack() as stack:
        read_fd, write_fd = os.pipe()
        stack.callback(_safe_close, read_fd)
        stack.callback(_safe_close, write_fd)
        _feed_pipe(write_fd, payload)
        # Close the writer so the consumer observes EOF; the ExitStack's
        # second close of the same descriptor is a harmless no-op.
        _safe_close(write_fd)
        return typ.cast(
            "str",
            rust_streams.rust_consume_stream(read_fd, **kwargs),
        )


def _pump_via_pipes(
    rust_streams: ModuleType,
    payload: bytes,
    **kwargs: object,
) -> int:
    """Pump ``payload`` from a source pipe to a sink pipe; return bytes written."""
    with contextlib.ExitStack() as stack:
        src_read, src_write = os.pipe()
        sink_read, sink_write = os.pipe()
        for fd in (src_read, src_write, sink_read, sink_write):
            stack.callback(_safe_close, fd)
        _feed_pipe(src_write, payload)
        # Close the source writer so the pump reaches EOF; the payload is small
        # enough to fit the sink pipe buffer without draining `sink_read`.
        _safe_close(src_write)
        return int(rust_streams.rust_pump_stream(src_read, sink_write, **kwargs))


@_SUPPRESS_FIXTURE
@given(payload=st.binary(max_size=96))
def test_default_buffer_matches_explicit(
    rust_streams: ModuleType,
    payload: bytes,
) -> None:
    """Omitting ``buffer_size`` equals passing the explicit default."""
    explicit = _consume_via_pipe(
        rust_streams, payload, buffer_size=_DEFAULT_BUFFER_SIZE
    )
    default = _consume_via_pipe(rust_streams, payload)
    assert explicit == default, (
        "omitting buffer_size must equal the explicit 65536 default"
    )
    assert default == payload.decode("utf-8", errors="replace"), (
        "decoded output must match Python's UTF-8 replace decoding"
    )


@_SUPPRESS_FIXTURE
@given(payload=st.binary(max_size=96))
def test_pump_default_buffer_matches_explicit(
    rust_streams: ModuleType,
    payload: bytes,
) -> None:
    """``rust_pump_stream`` omitting ``buffer_size`` equals the explicit default."""
    explicit = _pump_via_pipes(rust_streams, payload, buffer_size=_DEFAULT_BUFFER_SIZE)
    default = _pump_via_pipes(rust_streams, payload)
    assert explicit == default, (
        "omitting buffer_size must equal the explicit 65536 default"
    )
    assert default == len(payload), "the pump must transfer every payload byte"
