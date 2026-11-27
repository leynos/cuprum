"""Public exports and runtime-resolvable annotations for live byte streams.

The five new names must be reachable from both ``cuprum`` and ``cuprum.sh``,
and each must resolve to the one object its defining module owns. Pinning
identity rather than presence is what stops a facade from growing a second
class with the same name, which would make an ``except LiveStreamError`` in a
caller miss the error the library raises.
"""

from __future__ import annotations

import typing as typ

import pytest

import cuprum as c
from cuprum import _process_handle_errors, byte_streams, sh
from cuprum._byte_stream import handle as handle_module
from cuprum._byte_stream import reader as reader_module

_LIVE_NAMES = (
    "ByteChunk",
    "ByteStreamReader",
    "LiveStreamError",
    "RunningBytesCommand",
    "StreamClosure",
)


@pytest.mark.parametrize("name", _LIVE_NAMES)
def test_live_names_are_exported_from_both_facades(name: str) -> None:
    """Every live-stream name is public from ``cuprum`` and ``cuprum.sh``."""
    assert hasattr(c, name), f"cuprum must export {name}"
    assert hasattr(sh, name), f"cuprum.sh must export {name}"
    assert name in c.__all__, f"cuprum.__all__ must name {name}"
    assert name in sh.__all__, f"cuprum.sh.__all__ must name {name}"
    assert getattr(c, name) is getattr(sh, name), (
        f"{name} must be one object across both facades"
    )


def test_live_names_resolve_to_their_defining_modules() -> None:
    """Each name resolves to the definition site, not a duplicate."""
    assert c.ByteChunk is byte_streams.ByteChunk, (
        "ByteChunk must come from byte_streams"
    )
    assert c.StreamClosure is byte_streams.StreamClosure, (
        "StreamClosure must come from byte_streams"
    )
    assert c.LiveStreamError is _process_handle_errors.LiveStreamError, (
        "LiveStreamError must come from the handle errors module"
    )
    assert c.ByteStreamReader is reader_module.ByteStreamReader, (
        "ByteStreamReader must come from the reader module"
    )
    assert c.RunningBytesCommand is handle_module.RunningBytesCommand, (
        "RunningBytesCommand must come from the handle module"
    )


def test_running_bytes_command_is_separate_from_the_text_handle() -> None:
    """The byte handle is a distinct type, not a widened text handle."""
    assert c.RunningBytesCommand is not c.RunningCommand, (
        "the byte handle must be separately typed"
    )
    assert not issubclass(c.RunningBytesCommand, c.RunningCommand), (
        "the byte handle must not masquerade as the text handle"
    )


def test_live_annotations_resolve_at_runtime() -> None:
    """The public annotations resolve through ``typing.get_type_hints``."""
    assert typ.get_type_hints(c.ByteChunk)["stream"] is c.EchoStream, (
        "ByteChunk.stream must resolve to EchoStream"
    )
    assert typ.get_type_hints(c.ByteChunk)["data"] is bytes, (
        "ByteChunk.data must resolve to bytes"
    )
    reader_hints = typ.get_type_hints(c.ByteStreamReader.__anext__)
    assert reader_hints["return"] is c.ByteChunk, (
        "ByteStreamReader.__anext__ must return ByteChunk"
    )
    wait_hints = typ.get_type_hints(c.RunningBytesCommand.wait)
    assert wait_hints["return"] is c.BytesCommandResult, (
        "RunningBytesCommand.wait must return BytesCommandResult"
    )
    start_hints = typ.get_type_hints(sh.SafeCmd.start_bytes)
    assert start_hints["return"] is c.RunningBytesCommand, (
        "SafeCmd.start_bytes must return RunningBytesCommand"
    )


def test_live_stream_error_reports_its_stream_and_closure() -> None:
    """The error carries the failed stream and the terminal status."""
    error = c.LiveStreamError(c.EchoStream.STDERR, cause=OSError("pipe"))
    assert error.stream is c.EchoStream.STDERR, "the error must name its stream"
    assert error.closure is c.StreamClosure.FAILED, "the error must report FAILED"
    assert isinstance(error.__cause__, OSError), "the cause must be chained"
