"""Helpers shared by the shared-stream-adapter behavioural scenarios.

The scenarios themselves live in
``test_streams_rs_adapter_contract_behaviour.py``. These values and call
helpers keep that module inside the repository's module-size limit without
moving any step definition away from pytest-bdd.
"""

from __future__ import annotations

import contextlib
import os
import typing as typ

import pytest

from cuprum.unittests._rust_stream_test_support import _OwnedDescriptor

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType


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


class _IndexEvidence(typ.NamedTuple):
    """The result and conversion counts of one index-like call."""

    result: object
    calls: tuple[int, ...]


class _ExtractionEvidence(typ.NamedTuple):
    """A refused argument plus the resources it must have left untouched."""

    error: BaseException
    reader_fd: int
    payload: bytes
    writer_fd: int | None


class _PlatformEvidence(typ.NamedTuple):
    """A platform-step failure plus the reader that must still hold its bytes."""

    error: BaseException
    reader_fd: int
    payload: bytes


def _open_writer() -> _OwnedDescriptor:
    """Open a writable descriptor the caller can hand to the pump."""
    return _OwnedDescriptor(os.open(os.devnull, os.O_WRONLY))


def _entry_point(adapters: ModuleType, operation: str) -> cabc.Callable[..., object]:
    """Return the named adapter entry point."""
    calls = {
        "pump": adapters.rust_pump_stream,
        "consume": adapters.rust_consume_stream,
    }
    return typ.cast("cabc.Callable[..., object]", calls[operation])


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
        writer: _OwnedDescriptor | None = None
        if operation == "pump":
            # The wrapper closes a writer that never reaches native ownership,
            # so the test must not close that number again on the way out.
            writer = _open_writer()
            stack.callback(writer.close)
            args = (descriptor, writer.fd)
        try:
            with pytest.raises(BaseException) as excinfo:  # ruff: ignore[pytest-raises-too-broad] - the Then step asserts the class.
                entry_point(*args, buffer_size=buffer_size)
        finally:
            if writer is not None:
                # The wrapper closes a writer that never reaches native
                # ownership on every failure path, so this test must not close
                # that number again.
                writer.disarm()
    return excinfo.value


def _feed_pipe(reader_fd: int, upstream: _OwnedDescriptor, payload: bytes) -> None:
    """Write the payload and close the source so the reader observes EOF."""
    os.write(upstream.fd, payload)
    upstream.close()
