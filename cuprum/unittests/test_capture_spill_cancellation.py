"""What a cancelled spill leaves behind for teardown to remove.

A spill is the one capture that creates a file the run owns. The file is made
with ``delete=False``, so from the moment it exists only the accumulator can
remove it, and a cancellation that arrives before the run can report a result
has no result to name it in. These are the cases that pin the two windows
where that happens: the create itself, and the first write that moves the
buffered prefix into the file.

Both cases assert the file is on disk before cleanup runs, then that cleanup
removed it, because a teardown assertion against an empty directory proves
nothing about what teardown did. ``keep_file=True`` is deliberate in both: the
caller's exemption covers a file the run completed and handed over, not one a
cancelled step left behind.
"""

from __future__ import annotations

import asyncio
import threading
import typing as typ

import pytest

from cuprum._capture_decode import _DecodeSettings
from cuprum._capture_file import _FileAccumulator
from cuprum.unittests._blocking_spill_file import (
    _STARTED_TIMEOUT_S,
    _await_blocked_write,
    _BlockingSpillFile,
)
from cuprum.unittests._capture_sink_support import _spilling_to

if typ.TYPE_CHECKING:
    import pathlib

_TEXT = _DecodeSettings(encoding="utf-8", errors="strict", capture_bytes=False)
_PAYLOAD = "中文 payload\n".encode()

# Long enough that reaching a deadlock is diagnosed as one, short enough that a
# test suite interrupted mid-case still ends on its own.
_BLOCKED_CREATE_TIMEOUT_S = 30.0


class _BlockingCreate:
    """A ``NamedTemporaryFile`` whose construction waits to be released.

    The free function that makes a spill file runs on a worker, so the only way
    to hold it is to block the thread. This factory signals from the worker that
    the create is in flight, then waits, which puts a cancellation in the window
    *before* the file exists rather than after.
    """

    def __init__(self, path: pathlib.Path) -> None:
        """Bind the path the file will have and start holding the create."""
        self.path = path
        self.started = False
        self._released = threading.Event()
        self.file: _BlockingSpillFile | None = None

    def __call__(self, **_: object) -> _BlockingSpillFile:
        """Signal the create is in flight, wait to be released, then create."""
        self.started = True
        if not self._released.wait(_BLOCKED_CREATE_TIMEOUT_S):
            pytest.fail("the blocked spill create was never released")
        self.file = _BlockingSpillFile(self.path)
        return self.file

    def release(self) -> None:
        """Let the blocked create make the file."""
        self._released.set()


async def _await_started_create(create: _BlockingCreate) -> None:
    """Fail the case unless the worker enters the create before the deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STARTED_TIMEOUT_S
    while loop.time() < deadline:
        if create.started:
            return
        await asyncio.sleep(0.01)
    pytest.fail("the spill never began creating its file")


def test_a_cancelled_create_still_leaves_cleanup_a_file_to_remove(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation while the file is being created must not orphan it.

    The create is the first thing a spill does, and ``delete=False`` makes the
    file ownerless the instant it exists. A cancellation that lands while the
    worker is still inside the create is therefore the narrowest orphan window
    there is: the file is made after the caller has already asked to stop, so
    no result can ever name it. Publishing the handle in a separate step after
    the create would leave it unreachable to teardown; publishing it inside the
    same shielded step is what closes the window.
    """
    create = _BlockingCreate(tmp_path / "cancelled-create")
    monkeypatch.setattr(
        "cuprum._capture_spill_io.NamedTemporaryFile",
        create,
    )
    accumulator = _FileAccumulator(
        policy=_spilling_to(tmp_path, threshold=1, keep_file=True),
        stream="stdout",
        settings=_TEXT,
    )

    async def cancel_the_create() -> None:
        """Cancel the absorb while the worker is still inside the create."""
        task = asyncio.create_task(accumulator.absorb(_PAYLOAD))
        await _await_started_create(create)
        task.cancel()
        create.release()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_the_create())

    assert create.file is not None, (
        "the case is vacuous unless the released create actually made a file"
    )
    assert list(tmp_path.iterdir()), (
        "this case needs the created file to still be on disk before cleanup"
    )
    asyncio.run(accumulator.cleanup())
    assert not list(tmp_path.iterdir()), (
        "teardown must remove a file whose create was cancelled; found "
        f"{list(tmp_path.iterdir())}"
    )


def test_a_cancelled_spill_leaves_cleanup_a_file_to_remove(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation between creating the file and writing it must not orphan it.

    The file is made with ``delete=False``, so from the moment it exists only
    the accumulator can remove it. If the handle were published only after the
    prefix was written and flushed, a cancellation during those awaits would
    leave a file no result names and no sink knows about — and teardown would
    have nothing to close or unlink.
    """
    blocking = _BlockingSpillFile(tmp_path / "cancelled-spill")
    monkeypatch.setattr(
        "cuprum._capture_spill_io.NamedTemporaryFile",
        lambda **_: blocking,
    )
    accumulator = _FileAccumulator(
        policy=_spilling_to(tmp_path, threshold=1, keep_file=True),
        stream="stdout",
        settings=_TEXT,
    )

    async def spill_then_cancel() -> None:
        """Cancel the absorb while its first write is still in flight."""
        task = asyncio.create_task(accumulator.absorb(_PAYLOAD))
        await _await_blocked_write(blocking, 1)
        task.cancel()
        blocking.release(1)
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(spill_then_cancel())

    assert list(tmp_path.iterdir()), (
        "this case needs the created file to still be on disk before cleanup"
    )
    asyncio.run(accumulator.cleanup())
    assert not list(tmp_path.iterdir()), (
        "teardown must remove a file created by a spill that was cancelled "
        f"before it wrote the prefix; found {list(tmp_path.iterdir())}"
    )
