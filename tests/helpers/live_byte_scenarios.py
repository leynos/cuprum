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
import shutil
import signal
import sys
import tempfile
import time
import typing as typ
from pathlib import Path

from cuprum import (
    ByteStreamReader,
    ExecutionContext,
    ProcessGroupPolicy,
    Program,
    ProgramCatalogue,
    StdinStream,
    StreamClosure,
    TimeoutExpired,
    sh,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_CATALOGUE = ProgramCatalogue.from_programs(sys.executable, name="live-scenarios")
_PYTHON = sh.make(Program(sys.executable), catalogue=_CATALOGUE)
_PREFIX = b"prefix:"
_PAYLOAD_SIZE = 128 * 1024
_SETTLEMENT_DEADLINE_S = 8.0
_DESCENDANT_WAIT_S = 3.0
_REAP_TIMEOUT_S = 2.0
_HANDLE_DEADLINE_S = 10.0
_ARGV_LENGTH = 2


def _require(
    condition: bool,  # ruff: ignore[boolean-type-hint-positional-argument] - a predicate and its explanation are both required; keyword-only would only lengthen every call.
    message: str,
) -> None:
    """Raise when a scenario observation does not hold."""
    if not condition:
        raise RuntimeError(message)


async def _read_all(reader: ByteStreamReader) -> bytes:
    """Consume one reader to its end and return the reconstructed bytes."""
    received = bytearray()
    async for chunk in reader:
        received.extend(chunk.data)
    return bytes(received)


async def _read_until(reader: ByteStreamReader, marker: bytes) -> tuple[bytes, bytes]:
    """Consume until *marker* appears, returning head and leftover bytes."""
    collected = bytearray()
    async for chunk in reader:
        collected.extend(chunk.data)
        if marker in collected:
            end = collected.index(marker) + len(marker)
            return bytes(collected[:end]), bytes(collected[end:])
    message = f"stream ended before {marker!r}: {bytes(collected)!r}"
    raise RuntimeError(message)


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
    """Reconstruct large output from both pipes under a small byte bound."""
    code = (
        "import sys\n"
        f"payload = b'x' * {_PAYLOAD_SIZE}\n"
        "sys.stdout.buffer.write(payload)\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(payload)\n"
        "sys.stderr.buffer.flush()\n"
    )
    async with await _PYTHON("-c", code).start_bytes(max_buffered_bytes=1024) as handle:
        out, err = await asyncio.gather(
            _read_all(handle.stdout),
            _read_all(handle.stderr),
        )
        result = await handle.wait()
    _require(len(out) == _PAYLOAD_SIZE, f"stdout length was {len(out)}")
    _require(len(err) == _PAYLOAD_SIZE, f"stderr length was {len(err)}")
    _require(result.stdout == out, "capture must equal the live stdout bytes")
    _require(result.stderr == err, "capture must equal the live stderr bytes")


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


def _is_alive(pid: int) -> bool:
    """Return whether *pid* still exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float) -> bool:
    """Wait up to *timeout* seconds for *pid* to disappear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(0.05)
    return not _is_alive(pid)


def _kill_and_reap(pid: int) -> None:
    """Kill a surviving descendant and wait for it to disappear."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    _wait_dead(pid, _REAP_TIMEOUT_S)


def _descendant_child_code(marker: Path) -> str:
    """Build the leader's code: spawn a pipe-holding descendant, then exit."""
    descendant = "import time; time.sleep(30)"
    return (
        "import subprocess, sys\n"
        "child = subprocess.Popen(\n"
        f"    [sys.executable, '-c', {descendant!r}],\n"
        "    stdout=sys.stdout.fileno(),\n"
        "    stderr=sys.stderr.fileno(),\n"
        ")\n"
        f"with open({str(marker)!r}, 'w') as stream:\n"
        "    stream.write(str(child.pid))\n"
        "print('leader-done', flush=True)\n"
    )


async def _descendant_scenario(*, own_group: bool) -> None:
    """Settle a run whose child leaves a descendant holding the output pipe."""
    directory = Path(tempfile.mkdtemp(prefix="cuprum-live-"))
    marker = directory / "descendant.pid"
    code = _descendant_child_code(marker)
    policy = ProcessGroupPolicy.OWN_GROUP if own_group else ProcessGroupPolicy.INHERIT
    context = ExecutionContext(process_group=policy, cancel_grace=0.2)
    handle = await _PYTHON("-c", code).start_bytes(context=context)
    started = time.monotonic()
    result = await handle.wait(timeout=_HANDLE_DEADLINE_S)
    elapsed = time.monotonic() - started
    _require(elapsed < _SETTLEMENT_DEADLINE_S, f"settlement took {elapsed:.2f}s")
    _require(result.ok, f"the leader exited {result.exit_code}")
    _require(marker.is_file(), "the descendant PID marker was not written")
    descendant = int(marker.read_text(encoding="utf-8"))
    try:
        if own_group:
            _require(
                _wait_dead(descendant, _DESCENDANT_WAIT_S),
                "an owned group must reap the descendant holding the pipe",
            )
            _require(
                handle.stdout.closure is StreamClosure.EOF,
                "the group stop should let stdout reach EOF, got "
                f"{handle.stdout.closure!r}",
            )
        else:
            _require(_is_alive(descendant), "an inherited descendant must survive")
            _require(
                handle.stdout.closure is StreamClosure.TRUNCATED_BY_SETTLEMENT,
                "an inherited descendant must truncate the reader, got "
                f"{handle.stdout.closure!r}",
            )
    finally:
        # The survival assertion above must run before this cleanup; a failed
        # assertion must not leave a 30-second sleeper behind.
        _kill_and_reap(descendant)
        shutil.rmtree(directory, ignore_errors=True)


async def descendant_own_group() -> None:
    """Reap a descendant that holds the pipe when the run owns its group."""
    await _descendant_scenario(own_group=True)


async def descendant_inherit() -> None:
    """Leave a descendant alive when the run inherits the parent's group."""
    await _descendant_scenario(own_group=False)


_SCENARIOS = {
    "prefix_before_input": prefix_before_input,
    "independent_eof": independent_eof,
    "malformed_split_utf8": malformed_split_utf8,
    "large_simultaneous": large_simultaneous,
    "stalled_then_wait": stalled_then_wait,
    "cancel_while_blocked": cancel_while_blocked,
    "close_one_stream": close_one_stream,
    "descendant_own_group": descendant_own_group,
    "descendant_inherit": descendant_inherit,
}


def main(argv: list[str]) -> int:
    """Dispatch one scenario by name and report success on standard output."""
    if len(argv) != _ARGV_LENGTH or argv[1] not in _SCENARIOS:
        print(f"usage: {argv[0]} <{'|'.join(sorted(_SCENARIOS))}>", file=sys.stderr)
        return 2
    name = argv[1]
    asyncio.run(_SCENARIOS[name]())
    print(f"SCENARIO-OK:{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
