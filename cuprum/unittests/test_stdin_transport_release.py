"""A stalled stdin close must release its descriptor, not merely stop waiting.

Bounding the close is not the same as finishing it, and the difference is a
leaked descriptor per stalled run. ``StreamWriter.close`` delegates to the
transport's ``write_eof``, and CPython's ``_UnixWritePipeTransport`` completes
the shutdown *only when its buffer is empty*: with bytes still buffered it sets
``_closing`` and returns, leaving the descriptor open and the transport
registered with the loop. Where the reader never drains — the stalled-close
case the bounded window exists for — nothing follows to finish the job.

``abort()`` is the transport's unconditional release: it clears the buffer and
closes the descriptor. These tests drive a real pipe into the non-empty-buffer
state and assert on the transport's own attributes, because the graceful path
and the abort are indistinguishable in every other observable.
"""

from __future__ import annotations

import asyncio
import os
import sys
import typing as typ

import pytest

from cuprum._subprocess_stdin import _close_stdin
from cuprum.unittests.test_line_event_emission_support import _make_observation

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent

# The child holds the read end of its own stdin open past the point where the
# parent gives up: a grandchild inherits the descriptor and sleeps. Nothing
# reads it either, so no ``EPIPE`` can arrive to end the write — the pipe is
# simply stuck, which is what makes the graceful close incomplete.
_GRANDCHILD_LIFETIME_S = 30
_HOLD_THE_READ_END = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([\n"
    "    sys.executable,\n"
    "    '-c',\n"
    f"    'import time; time.sleep({_GRANDCHILD_LIFETIME_S})',\n"
    "])\n"
    f"time.sleep({_GRANDCHILD_LIFETIME_S})\n"
)

# Comfortably past the 64 KiB pipe buffer, so the write cannot drain and the
# transport is left holding a non-empty buffer — the state the fix is for.
_BLOCKING_PAYLOAD_BYTES = 1 << 20


async def _stalled_writer() -> tuple[asyncio.subprocess.Process, asyncio.StreamWriter]:
    """Start a child whose stdin pipe can neither drain nor close.

    Returns
    -------
    tuple[asyncio.subprocess.Process, asyncio.StreamWriter]
        The child and a stdin writer whose transport still holds buffered
        bytes. The caller must kill the child: with the read end held open by
        a descendant, neither end can finish on its own within the test's
        lifetime.

    The non-vacuity precondition is asserted rather than assumed. Every
    assertion the caller makes is about what a *non-empty* buffer leaves
    behind; with the buffer drained the graceful close finishes the shutdown
    by itself and no observable tells the two paths apart. A vacuous scenario
    therefore fails here, where the cause is visible, instead of passing below.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _HOLD_THE_READ_END,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    writer = process.stdin
    assert writer is not None, "the child was started with a stdin pipe"
    writer.write(b"z" * _BLOCKING_PAYLOAD_BYTES)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(writer.drain(), 1.0)
    buffered = len(getattr(writer.transport, "_buffer", b""))
    assert buffered > 0, (
        "the transport drained, so the graceful close would finish on its own "
        "and this scenario could not tell the two paths apart"
    )
    return process, writer


def _descriptor_is_open(descriptor: int) -> bool:
    """Whether *descriptor* still names something in this process."""
    try:
        os.fstat(descriptor)
    except OSError:
        return False
    return True


def test_a_stalled_close_releases_the_descriptor() -> None:
    """The close a bounded wait gives up on still frees what the pipe held.

    Without the abort the transport keeps all of its buffered bytes and its
    descriptor stays open for the life of the process: the graceful close sets
    ``_closing``, finds the buffer non-empty, and stops. The run's own deadline
    expires, the child is killed, and the descriptor outlives both.

    The loop is given a turn before the assertions because the release runs
    from ``call_soon``; reading immediately would inspect the transport before
    the fix has had the chance it provides.
    """
    events: list[ExecEvent] = []
    observation = _make_observation(observe_hooks=(events.append,))

    async def scenario() -> tuple[int, bool, int]:
        """Close a stalled pipe and report its buffer and descriptor state."""
        process, writer = await _stalled_writer()
        # The descriptor is reached through ``get_extra_info`` rather than the
        # transport's own ``_fileno``, which is private and untyped. The
        # published accessor returns the same ``FileIO`` the transport holds for
        # its pipe, and it narrows the attribute's type: ``get_extra_info`` is
        # declared to return ``Any``, so the cast states what the pipe key
        # actually carries. That combination is what lets one read satisfy the
        # linters and the type checker at once, which neither a direct private
        # read nor ``getattr`` with a constant name manages.
        pipe = typ.cast("typ.IO[bytes]", writer.transport.get_extra_info("pipe"))
        descriptor = pipe.fileno()
        try:
            await _close_stdin(process, writer, observation)
            await asyncio.sleep(0.1)
            # ``_buffer`` is read with a default because the private attribute
            # disappears on some transport paths; its absence means no bytes are
            # retained, which is the state the assertion below requires.
            return (
                len(getattr(writer.transport, "_buffer", b"")),
                _descriptor_is_open(descriptor),
                descriptor,
            )
        finally:
            process.kill()
            await process.wait()

    buffered, open_after, descriptor = asyncio.run(scenario())

    assert not open_after, (
        f"descriptor {descriptor} is still open after the close gave up on it; "
        "the transport kept the pipe alive rather than releasing it"
    )
    assert buffered == 0, (
        f"the transport still holds {buffered} buffered bytes after the close; "
        "an abort discards them, so a retained buffer means it never ran"
    )
    assert [event.phase for event in events] == [], (
        "the expired window is not a pipe fault: cuprum wrote everything it "
        "was given and the reader outlived the child, so no diagnostic is due"
    )
