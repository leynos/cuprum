"""Held-handle failure scenarios for owned live byte streams.

A live failure must stop and reap a child that nobody is waiting on. These
stories cover the two failure sources a held handle can see: a drain whose
echo sink fails, and a stdin producer that raises while the child ignores its
input. Both observe the child's raw PID, so the calling tests mark them
POSIX-only.

The scenarios live apart from the portable stories so each helper module stays
inside the repository's line ceiling.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ

from cuprum import (
    EchoStream,
    ExecutionContext,
    LiveStreamError,
    RunOutputOptions,
    StdinSourceError,
    StdinStream,
    StreamClosure,
)
from tests.helpers.live_byte_support import (
    PYTHON as _PYTHON,
)
from tests.helpers.live_byte_support import (
    is_alive as _is_alive,
)
from tests.helpers.live_byte_support import (
    kill_and_reap as _kill_and_reap,
)
from tests.helpers.live_byte_support import (
    require as _require,
)
from tests.helpers.live_byte_support import (
    wait_dead as _wait_dead,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["live_failure_stops_held_child", "stdin_failure_stops_held_child"]


class _FailingSink(io.StringIO):
    """A text sink whose write always fails, failing the drain that echoes."""

    def write(self, _text: str) -> int:  # ruff: ignore[no-self-use] - overrides IO.write; the failure is the whole behaviour.
        """Fail the echo write."""
        message = "sink failed"
        raise ValueError(message)


async def live_failure_stops_held_child() -> None:
    """Stop and reap a held child when its live drain fails."""
    code = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'out\\n')\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n"
    )
    handle = await _PYTHON("-c", code).start_bytes(
        output=RunOutputOptions(echo=True),
        context=ExecutionContext(
            stdout_sink=_FailingSink(),
            cancel_grace=0.2,
        ),
    )
    pid = handle.pid
    if pid is None:
        message = "the child must have a PID"
        raise RuntimeError(message)
    try:
        try:
            await asyncio.wait_for(handle.stdout.wait_eof(), timeout=10)
        except LiveStreamError as error:
            _require(
                error.stream is EchoStream.STDOUT, "stdout must be the failed stream"
            )
        else:
            message = "the reader should have reported the drain failure"
            raise RuntimeError(message)
        reaped = await asyncio.to_thread(_wait_dead, pid, 10.0)
        _require(
            reaped,
            "a held handle must stop and reap the child when its drain fails",
        )
        try:
            await handle.wait()
        except LiveStreamError:
            pass
        else:
            message = "settlement should report the drain failure"
            raise RuntimeError(message)
        _require(
            handle.stderr.closure
            in {StreamClosure.EOF, StreamClosure.TRUNCATED_BY_SETTLEMENT},
            "the other stream must settle normally, not fail, got "
            f"{handle.stderr.closure!r}",
        )
    finally:
        if _is_alive(pid):
            _kill_and_reap(pid)


async def stdin_failure_stops_held_child() -> None:
    """Stop and reap a held child when its stdin producer fails."""

    async def produce() -> cabc.AsyncIterator[bytes]:
        """Yield one chunk, then fail while the child ignores stdin."""
        yield b"first"
        await asyncio.sleep(0)
        message = "producer failed"
        raise RuntimeError(message)

    code = "import time; time.sleep(30)"
    handle = await _PYTHON("-c", code).start_bytes(
        stdin=StdinStream(produce()),
        context=ExecutionContext(cancel_grace=0.2),
    )
    pid = handle.pid
    if pid is None:
        message = "the child must have a PID"
        raise RuntimeError(message)
    try:
        reaped = await asyncio.to_thread(_wait_dead, pid, 10.0)
        _require(
            reaped,
            "a held handle must stop and reap the child when its stdin producer fails",
        )
        try:
            await handle.wait()
        except StdinSourceError:
            pass
        else:
            message = "settlement should report the producer failure"
            raise RuntimeError(message)
    finally:
        if _is_alive(pid):
            _kill_and_reap(pid)
