"""Scripted real-child scenarios for owned live byte streams.

Each scenario is a complete exercise of one contract: causal exchange through
stdin and stdout, an EOF observed while another stream and the child remain
live, malformed and split UTF-8, large simultaneous output under a small
queue, a stalled consumer, cancellation while blocked, closing one reader, and
POSIX process-group containment when a descendant holds a pipe open.

Scenarios run in a separate interpreter under
:mod:`tests.helpers.supervised_scenario`, so a hang is killed with the whole
session rather than abandoned. They communicate success by exiting zero; a
raised ``RuntimeError`` carries the failing observation.
"""

from __future__ import annotations

import asyncio
import os
import sys
import typing as typ

from cuprum import (
    ExecutionContext,
    StdinStream,
    StreamClosure,
    TimeoutExpired,
)
from tests.helpers.live_byte_blocking_scenarios import (
    cancel_context_while_output_blocked,
    cancel_waiter_while_stdin_blocked,
    stalled_then_resumed,
)
from tests.helpers.live_byte_descendant_scenarios import (
    descendant_inherit,
    descendant_own_group,
)
from tests.helpers.live_byte_failure_scenarios import (
    live_failure_stops_held_child,
    stdin_failure_stops_held_child,
)
from tests.helpers.live_byte_support import HANDLE_DEADLINE_S as _HANDLE_DEADLINE_S
from tests.helpers.live_byte_support import PAYLOAD_SIZE as _PAYLOAD_SIZE
from tests.helpers.live_byte_support import PYTHON as _PYTHON
from tests.helpers.live_byte_support import read_all as _read_all
from tests.helpers.live_byte_support import read_until as _read_until
from tests.helpers.live_byte_support import require as _require

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_PREFIX = b"prefix:"
_ARGV_LENGTH = 2


async def prefix_before_input() -> None:
    """Observe a stdout prefix, then release a stdin producer through a barrier."""
    released = asyncio.Event()

    async def produce() -> cabc.AsyncIterator[bytes]:
        """Yield the token only after the prefix has been observed."""
        await released.wait()
        yield b"token\n"

    code = (
        "import sys\n"
        "sys.stdout.buffer.write(b'prefix:')\n"
        "sys.stdout.buffer.flush()\n"
        "line = sys.stdin.buffer.readline()\n"
        "sys.stdout.buffer.write(b'after:' + line)\n"
        "sys.stdout.buffer.flush()\n"
    )
    handle = await _PYTHON("-c", code).start_bytes(
        stdin=StdinStream(produce()),
        context=ExecutionContext(cancel_grace=0.2),
    )
    head, tail = await _read_until(handle.stdout, _PREFIX)
    _require(handle.result is None, "the child must still be live at the barrier")
    released.set()
    rest = bytearray(tail)
    async for chunk in handle.stdout:
        rest.extend(chunk.data)
    result = await handle.wait()
    live = head + bytes(rest)
    _require(live == b"prefix:after:token\n", f"live bytes were {live!r}")
    _require(result.stdout == live, "capture must equal the live bytes")
    _require(result.ok, f"the child exited {result.exit_code}")


async def independent_eof() -> None:
    """Observe stdout EOF while stderr and the child remain live."""
    released = asyncio.Event()

    async def produce() -> cabc.AsyncIterator[bytes]:
        """Yield the stderr token only after stdout's EOF is observed."""
        await released.wait()
        yield b"go\n"

    code = (
        "import os, sys\n"
        "sys.stdout.buffer.write(b'out')\n"
        "sys.stdout.buffer.flush()\n"
        "null = os.open(os.devnull, os.O_WRONLY)\n"
        "os.dup2(null, 1)\n"
        "sys.stdin.buffer.readline()\n"
        "sys.stderr.buffer.write(b'err')\n"
        "sys.stderr.buffer.flush()\n"
    )
    async with await _PYTHON("-c", code).start_bytes(
        stdin=StdinStream(produce()),
    ) as handle:
        out = await _read_all(handle.stdout)
        _require(out == b"out", f"stdout was {out!r}")
        _require(handle.stdout.closure is StreamClosure.EOF, "stdout must reach EOF")
        _require(handle.stderr.closure is None, "stderr must remain live")
        _require(handle.result is None, "the child must still be running")
        released.set()
        err = await _read_all(handle.stderr)
        result = await handle.wait()
    _require(err == b"err", f"stderr was {err!r}")
    _require(result.stderr == b"err", "capture must equal the live bytes")


async def malformed_split_utf8() -> None:
    """Reconstruct invalid UTF-8 and a multibyte character split across reads."""
    code = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'\\xff')\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(0.05)\n"
        "sys.stdout.buffer.write(b'\\xe2')\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(0.05)\n"
        "sys.stdout.buffer.write(b'\\x82\\xac\\n')\n"
        "sys.stdout.buffer.flush()\n"
    )
    expected = b"\xff\xe2\x82\xac\n"
    async with await _PYTHON("-c", code).start_bytes() as handle:
        live = await _read_all(handle.stdout)
        result = await handle.wait()
    _require(live == expected, f"live bytes were {live!r}")
    _require(result.stdout == expected, f"captured bytes were {result.stdout!r}")


async def large_simultaneous() -> None:
    """Reconstruct both pipes written concurrently under a small byte bound."""
    code = (
        "import sys, threading\n"
        f"out = bytes(range(256)) * {_PAYLOAD_SIZE // 256}\n"
        f"err = bytes(reversed(range(256))) * {_PAYLOAD_SIZE // 256}\n"
        "def write(stream, data):\n"
        "    stream.write(data)\n"
        "    stream.flush()\n"
        "writer = threading.Thread\n"
        "writer(target=write, args=(sys.stdout.buffer, out)).start()\n"
        "writer(target=write, args=(sys.stderr.buffer, err)).start()\n"
    )
    expected_out = bytes(range(256)) * (_PAYLOAD_SIZE // 256)
    expected_err = bytes(reversed(range(256))) * (_PAYLOAD_SIZE // 256)
    async with await _PYTHON("-c", code).start_bytes(max_buffered_bytes=1024) as handle:
        out, err = await asyncio.gather(
            _read_all(handle.stdout),
            _read_all(handle.stderr),
        )
        result = await handle.wait()
    _require(
        out == expected_out,
        f"live stdout differed at {_first_difference(out, expected_out)}",
    )
    _require(
        err == expected_err,
        f"live stderr differed at {_first_difference(err, expected_err)}",
    )
    _require(
        result.stdout == expected_out, "capture must equal the expected stdout bytes"
    )
    _require(
        result.stderr == expected_err, "capture must equal the expected stderr bytes"
    )


def _first_difference(actual: bytes, expected: bytes) -> str:
    """Describe the first differing offset, without dumping either payload."""
    for index, (left, right) in enumerate(zip(actual, expected, strict=False)):
        if left != right:
            return f"offset {index}: {left:#04x} != {right:#04x}"
    return f"lengths {len(actual)} != {len(expected)}"


async def stalled_then_wait() -> None:
    """Settle a run whose consumer never reads, without losing the capture."""
    code = (
        "import sys\n"
        f"sys.stdout.buffer.write(b'x' * {_PAYLOAD_SIZE})\n"
        "sys.stdout.buffer.flush()\n"
    )
    handle = await _PYTHON("-c", code).start_bytes(max_buffered_bytes=1024)
    result = await handle.wait(timeout=_HANDLE_DEADLINE_S)
    _require(
        handle.stdout.closure is StreamClosure.TRUNCATED_BY_SETTLEMENT,
        (f"a stalled reader must report truncation, got {handle.stdout.closure!r}"),
    )
    _require(
        result.stdout is not None and len(result.stdout) == _PAYLOAD_SIZE,
        ("the capture must survive a stalled consumer"),
    )
    _require(result.ok, f"the child exited {result.exit_code}")


async def cancel_while_blocked() -> None:
    """Preserve partial output when a deadline fires with a blocked writer."""
    code = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'prefix')\n"
        "sys.stdout.buffer.flush()\n"
        f"sys.stdout.buffer.write(b'x' * {_PAYLOAD_SIZE})\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n"
    )
    handle = await _PYTHON("-c", code).start_bytes(max_buffered_bytes=1024)
    head, _tail = await asyncio.wait_for(
        _read_until(handle.stdout, b"prefix"),
        timeout=_HANDLE_DEADLINE_S,
    )
    _require(head == b"prefix", f"the prefix was {head!r}")
    try:
        await handle.wait(timeout=0.5)
    except TimeoutExpired as exc:
        partial = exc.output
        if not isinstance(partial, bytes):
            message = f"a byte run must report byte output, got {type(partial)!r}"
            raise TypeError(message) from None
        _require(partial.startswith(b"prefix"), f"partial output was {partial[:16]!r}")
    else:
        message = "the wait should have expired"
        raise RuntimeError(message)
    _require(handle.result is not None, "the stopped child must have a recorded result")
    _require(
        handle.stdout.closure
        in {StreamClosure.EOF, StreamClosure.TRUNCATED_BY_SETTLEMENT},
        f"the reader ended as {handle.stdout.closure!r}",
    )


async def close_one_stream() -> None:
    """Close stdout while stderr and the child continue to completion."""
    code = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'out')\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(0.5)\n"
        "sys.stderr.buffer.write(b'err')\n"
        "sys.stderr.buffer.flush()\n"
    )
    async with await _PYTHON("-c", code).start_bytes() as handle:
        await handle.stdout.aclose()
        err = await _read_all(handle.stderr)
        result = await handle.wait()
    _require(
        handle.stdout.closure is StreamClosure.READER_CLOSED,
        (f"the closed reader ended as {handle.stdout.closure!r}"),
    )
    _require(err == b"err", f"stderr was {err!r}")
    _require(result.ok, f"the child exited {result.exit_code}")


_SCENARIOS = {
    "prefix_before_input": prefix_before_input,
    "independent_eof": independent_eof,
    "malformed_split_utf8": malformed_split_utf8,
    "large_simultaneous": large_simultaneous,
    "stalled_then_wait": stalled_then_wait,
    "cancel_while_blocked": cancel_while_blocked,
    "close_one_stream": close_one_stream,
    "cancel_context_while_output_blocked": cancel_context_while_output_blocked,
    "cancel_waiter_while_stdin_blocked": cancel_waiter_while_stdin_blocked,
    "stalled_then_resumed": stalled_then_resumed,
    "live_failure_stops_held_child": live_failure_stops_held_child,
    "stdin_failure_stops_held_child": stdin_failure_stops_held_child,
    "descendant_own_group": descendant_own_group,
    "descendant_inherit": descendant_inherit,
}


def _wait4_unavailable() -> bool:
    """Report the asyncio fallback route for this scenario run."""
    return False


def main(argv: list[str]) -> int:
    """Dispatch one scenario by name and report success on standard output."""
    if len(argv) != _ARGV_LENGTH or argv[1] not in _SCENARIOS:
        print(f"usage: {argv[0]} <{'|'.join(sorted(_SCENARIOS))}>", file=sys.stderr)
        return 2
    name = argv[1]
    if os.environ.get("CUPRUM_LIVE_SCENARIO_WAIT_ROUTE") == "fallback":
        from cuprum import _wait4_spawn

        # Assigned through the module mapping: a plain attribute assignment
        # is rejected by the type checker, and `setattr` is rewritten back to
        # one by the formatter.
        vars(_wait4_spawn)["wait4_resource_measurement_available"] = _wait4_unavailable
    asyncio.run(_SCENARIOS[name]())
    print(f"SCENARIO-OK:{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
