"""Property-based boundary tests for the Rust stream entry points.

The example-based suite in ``test_rust_streams.py`` covers a curated set of
buffer sizes and I/O failures. These properties fuzz the argument-validation
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

import os
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.helpers.stream_pipes import _safe_close

if typ.TYPE_CHECKING:
    from types import ModuleType

# Mirror of MAX_BUFFER_SIZE in rust/cuprum-rust/src/lib.rs (1 GiB).
_MAX_BUFFER_SIZE = 1 << 30
_DEFAULT_BUFFER_SIZE = 65536
_I32_MAX = (1 << 31) - 1
_I64_MAX = (1 << 63) - 1
# A descriptor value that is guaranteed to be closed/invalid but non-negative,
# used only where validation fails before the descriptor is dereferenced.
_UNUSED_FD = 0

_SUPPRESS_FIXTURE = settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=50,
)


@_SUPPRESS_FIXTURE
@given(bad_size=st.integers(min_value=-(1 << 62), max_value=0))
def test_consume_rejects_nonpositive_buffer(
    rust_streams: ModuleType,
    bad_size: int,
) -> None:
    """A non-positive ``buffer_size`` raises ``ValueError`` before any read."""
    with pytest.raises(ValueError, match="buffer_size"):
        rust_streams.rust_consume_stream(_UNUSED_FD, buffer_size=bad_size)


@_SUPPRESS_FIXTURE
@given(bad_size=st.integers(min_value=_MAX_BUFFER_SIZE + 1, max_value=_I64_MAX))
def test_consume_rejects_oversized_buffer(
    rust_streams: ModuleType,
    bad_size: int,
) -> None:
    """A ``buffer_size`` above the 1 GiB cap raises ``ValueError``."""
    with pytest.raises(ValueError, match="buffer_size"):
        rust_streams.rust_consume_stream(_UNUSED_FD, buffer_size=bad_size)


@_SUPPRESS_FIXTURE
@given(bad_size=st.integers(min_value=-(1 << 62), max_value=0))
def test_pump_rejects_nonpositive_buffer(
    rust_streams: ModuleType,
    bad_size: int,
) -> None:
    """``rust_pump_stream`` rejects a non-positive ``buffer_size``."""
    with pytest.raises(ValueError, match="buffer_size"):
        rust_streams.rust_pump_stream(_UNUSED_FD, _UNUSED_FD, buffer_size=bad_size)


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
    """``rust_pump_stream`` rejects an invalid reader descriptor."""
    with pytest.raises(ValueError, match="file descriptor"):
        rust_streams.rust_pump_stream(bad_fd, _UNUSED_FD)


def _consume_via_pipe(
    rust_streams: ModuleType,
    payload: bytes,
    **kwargs: object,
) -> str:
    """Write ``payload`` through a pipe and consume it with the Rust decoder."""
    read_fd, write_fd = os.pipe()
    try:
        view = memoryview(payload)
        while view:
            written = os.write(write_fd, view)
            view = view[written:]
        _safe_close(write_fd)
        write_fd = -1
        return typ.cast(
            "str",
            rust_streams.rust_consume_stream(read_fd, **kwargs),
        )
    finally:
        _safe_close(read_fd)
        if write_fd != -1:
            _safe_close(write_fd)


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
    assert explicit == default
    assert default == payload.decode("utf-8", errors="replace")
