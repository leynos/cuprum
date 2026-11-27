"""Static-check fixture for the owned live byte-stream surface.

This module is checked by ``ty`` but is never imported or executed. It pins the
reader, chunk, closure, and byte-result annotations a caller sees, so a change
that widens or misnames any of them fails ``make typecheck``.
"""

from __future__ import annotations

import typing as typ

from cuprum import (
    ECHO,
    ByteChunk,
    BytesCommandResult,
    ByteStreamReader,
    EchoStream,
    RunningBytesCommand,
    StreamClosure,
    sh,
)


async def live_byte_stream_types_resolve() -> None:
    """Assert every public annotation the live surface exposes."""
    command = sh.make(ECHO)("-n", "x")
    handle = await command.start_bytes()
    typ.assert_type(handle, RunningBytesCommand)
    typ.assert_type(handle.stdout, ByteStreamReader)
    typ.assert_type(handle.stderr, ByteStreamReader)
    typ.assert_type(handle.result, BytesCommandResult | None)
    async for chunk in handle.stdout:
        typ.assert_type(chunk, ByteChunk)
        typ.assert_type(chunk.stream, EchoStream)
        typ.assert_type(chunk.data, bytes)
        typ.assert_type(chunk.sequence, int)
    closure = await handle.stdout.wait_eof()
    typ.assert_type(closure, StreamClosure)
    result = await handle.wait()
    typ.assert_type(result, BytesCommandResult)
    typ.assert_type(result.stdout, bytes | None)
    typ.assert_type(result.stderr, bytes | None)
