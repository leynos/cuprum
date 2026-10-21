"""A spill file whose writes can be held open while a test acts.

The spill suite's cancellation cases all need the same thing: the worker call
the accumulator is awaiting must still be in flight when the test cancels, or
the cancellation lands somewhere else and the case quietly tests nothing. A
file that blocks its first write holds the transition between creating the file
and the buffered prefix reaching it; one that blocks a later write holds an
append, which is the window a running drain's teardown cancellation reaches on
a stream that has already spilled.

Which write is being held is exposed as :attr:`_BlockingSpillFile.blocked`, so
a test can wait for *the* write it means rather than for "some write", and each
gated write is released by its own number. The wait is bounded and its result
is asserted, because a missing release would otherwise surface as a hung suite
rather than as the broken case it is.
"""

from __future__ import annotations

import asyncio
import threading
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import pathlib

# Long enough that reaching a deadlock is diagnosed as one, short enough that a
# test suite interrupted mid-case still ends on its own.
_BLOCKED_WRITE_TIMEOUT_S = 30.0
# A generous cap next to the microseconds an idle event loop needs to notice a
# flag a worker set; it exists so a case that never arrives fails rather than
# hanging the session.
_STARTED_TIMEOUT_S = 10.0


class _BlockingSpillFile:
    """A spill file that holds the writes listed in *block_on* until released."""

    def __init__(self, path: pathlib.Path, *, block_on: tuple[int, ...] = (1,)) -> None:
        """Create *path*, so the file exists before the first write is entered."""
        self.name = str(path)
        # The write currently held, or ``0`` when none is, and the writes that
        # have been entered so far. Both are read by a polling test, so neither
        # is meaningful until the worker has set it.
        self.blocked = 0
        self.writes = 0
        self._events = {index: threading.Event() for index in block_on}
        self._file = path.open("wb")

    def write(self, data: bytes) -> int:
        """Signal when a gated write is in flight, then wait to be released."""
        self.writes += 1
        event = self._events.get(self.writes)
        if event is not None:
            self.blocked = self.writes
            if not event.wait(_BLOCKED_WRITE_TIMEOUT_S):
                pytest.fail(f"the spill write {self.writes} was never released")
            self.blocked = 0
        return self._file.write(data)

    def flush(self) -> None:
        """Flush the underlying file."""
        self._file.flush()

    def close(self) -> None:
        """Close the underlying file."""
        self._file.close()

    def release(self, write: int) -> None:
        """Let the gated *write* finish."""
        self._events[write].set()


async def _await_blocked_write(blocking: _BlockingSpillFile, write: int) -> None:
    """Fail the case unless *write* is being held before the deadline.

    The signal is set by a worker thread, which no asyncio primitive can
    observe, so polling is the coordination.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STARTED_TIMEOUT_S
    while loop.time() < deadline:
        if blocking.blocked == write:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"the spill never held its write {write} open")
