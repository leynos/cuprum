"""Cancellation and stall scenarios for owned live byte streams.

Each story here parks a real child against a bound it cannot pass without the
parent: a full live queue blocks the drain, a full stdin pipe blocks the
writer, or a small queue stalls delivery until the caller resumes reading. The
scenario then cancels, resumes, or settles through the owning handle and
checks that teardown leaves no task, descriptor, or unreaped child behind.

They live apart from the portable exchange stories so every helper module stays
inside the repository's 400-line ceiling. The supervisor runs them under the
same external watchdog as every other scenario.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum import ExecutionContext, StdinStream, StreamClosure
from tests.helpers.live_byte_support import (
    PYTHON as _PYTHON,
)
from tests.helpers.live_byte_support import (
    open_fd_count as _open_fd_count,
)
from tests.helpers.live_byte_support import (
    read_all as _read_all,
)
from tests.helpers.live_byte_support import (
    require as _require,
)
from tests.helpers.live_byte_support import (
    require_clean_teardown as _require_clean_teardown,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = [
    "cancel_context_while_output_blocked",
    "cancel_waiter_while_stdin_blocked",
    "stalled_then_resumed",
]

_STALL_PREFIX = b"0123456789abcdef"


async def cancel_context_while_output_blocked() -> None:
    """Cancel a context-held run while its live queue blocks the child."""
    code = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'x' * 131072)\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n"
    )
    before = _open_fd_count()
    handle = await _PYTHON("-c", code).start_bytes(max_buffered_bytes=1024)

    async def hold() -> None:
        """Own the handle until cancellation tears the block down."""
        async with handle:
            await asyncio.sleep(30)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.5)
    holder.cancel()
    try:
        await holder
    except asyncio.CancelledError:
        pass
    else:
        message = "the holder should have been cancelled"
        raise RuntimeError(message)
    _require(handle.result is not None, "teardown must settle the handle")
    _require(
        handle.stdout.closure
        in {StreamClosure.EOF, StreamClosure.TRUNCATED_BY_SETTLEMENT},
        f"the reader settled as {handle.stdout.closure!r}",
    )
    _require_clean_teardown(before)


async def cancel_waiter_while_stdin_blocked() -> None:
    """Cancel a waiter while the stdin writer is blocked on a full pipe."""
    finalized = asyncio.Event()

    async def produce() -> cabc.AsyncIterator[bytes]:
        """Offer more than the pipe can take, so the writer blocks in drain."""
        await asyncio.sleep(0)
        try:
            yield b"x" * (4 * 1024 * 1024)
            yield b"y"
        finally:
            finalized.set()

    before = _open_fd_count()
    handle = await _PYTHON("-c", "import time; time.sleep(30)").start_bytes(
        stdin=StdinStream(produce()),
        context=ExecutionContext(cancel_grace=0.2),
    )
    waiter = asyncio.create_task(handle.wait())
    await asyncio.sleep(0.5)
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass
    else:
        message = "the waiter should have been cancelled"
        raise RuntimeError(message)
    result = await handle.terminate(grace=0.2)
    await asyncio.wait_for(finalized.wait(), timeout=5.0)
    _require(result.exit_code in {-15, -9}, f"the child ended as {result.exit_code}")
    _require_clean_teardown(before)


async def stalled_then_resumed() -> None:
    """Resume lossless live delivery after the queue stalled the drain."""
    released = asyncio.Event()

    async def produce() -> cabc.AsyncIterator[bytes]:
        """Release the child's suffix only after the stalled prefix is read."""
        await released.wait()
        yield b"token\n"

    code = (
        "import sys\n"
        "sys.stdout.buffer.write(b'0123456789abcdef')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdin.buffer.readline()\n"
        "sys.stdout.buffer.write(b'-suffix')\n"
        "sys.stdout.buffer.flush()\n"
    )
    before = _open_fd_count()
    handle = await _PYTHON("-c", code).start_bytes(
        stdin=StdinStream(produce()),
        max_buffered_bytes=8,
    )
    # Widen the stall window: the 16-byte prefix cannot fit the 8-byte queue,
    # so the drain is blocked in put until this reader consumes the head.
    await asyncio.sleep(0.5)
    head = bytearray()
    async for chunk in handle.stdout:
        head.extend(chunk.data)
        if len(head) >= len(_STALL_PREFIX):
            break
    _require(
        bytes(head[: len(_STALL_PREFIX)]) == _STALL_PREFIX,
        f"the stalled prefix was {bytes(head)!r}",
    )
    released.set()
    rest = bytearray(await _read_all(handle.stdout))
    result = await handle.wait()
    live = bytes(head[: len(_STALL_PREFIX)]) + bytes(rest)
    _require(
        live == _STALL_PREFIX + b"-suffix",
        f"live bytes were {live!r}",
    )
    _require(result.stdout == live, "capture must equal the live bytes")
    _require_clean_teardown(before)
