"""Property-based round-trip tests for the Rust stream entry points.

The rejection boundary is covered by
``test_rust_streams_boundary_property.py``; these properties exercise the
opposite outcome. Each one drives a real transfer through a pipe and compares
the Rust entry point against an independently computed expectation:

- Omitting ``buffer_size`` is equivalent to passing the explicit default, for
  both ``rust_consume_stream`` and ``rust_pump_stream``.
- The decoded output and the transferred byte count match Python's own
  results, so "the default applies" cannot be satisfied by two identically
  wrong answers.

Every property here runs the entry point over a real pipe rather than a
throwaway descriptor, so the assertions hold on Windows as well as POSIX.
"""

from __future__ import annotations

import contextlib
import os
import typing as typ

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

if typ.TYPE_CHECKING:
    from types import ModuleType

# Mirror of the ``buffer_size = 65536`` signature default in
# rust/cuprum-rust/src/stream_pyfunctions.rs.
_DEFAULT_BUFFER_SIZE = 65536

_SUPPRESS_FIXTURE = settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=50,
)


def _safe_close(fd: int) -> None:
    """Close ``fd``, ignoring an already-closed or invalid descriptor."""
    with contextlib.suppress(OSError):
        os.close(fd)


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
