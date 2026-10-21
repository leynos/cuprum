"""Unit tests for stopping a stream drain inside a file-backed absorb.

A file-backed capture awaits a worker write for every chunk after its spill, so
``absorb`` is a real suspension point and a cancellation can land in it. These
cases pin what the drain reports when that happens; the drain's own contract
around read cancellation lives in ``test_stream_drain``.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ

from cuprum._streams import _drain, _StreamConfig
from cuprum.capture_policy import FileBackedCapture
from cuprum.unittests._blocking_spill_file import (
    _await_blocked_write,
    _BlockingSpillFile,
)

if typ.TYPE_CHECKING:
    import pathlib

    import pytest

    from cuprum.unittests._stream_drain_support import CapturedDrain


def _spill_config(directory: pathlib.Path) -> _StreamConfig:
    """Build a text config whose first chunk spills to *directory*."""
    return _StreamConfig(
        capture=FileBackedCapture(spill_threshold_bytes=None, directory=directory),
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="replace",
    )


def test_a_cancelled_absorb_retains_the_chunk_the_file_already_holds(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop during a spill write finalizes the prefix, exactly as a read stop does.

    The write is shielded, so the chunk has landed by the time the cancellation
    is re-raised; reporting an empty capture then would contradict a file that
    holds the bytes.

    The drain is cancelled while the append's write is held open, so the
    cancellation is guaranteed to arrive inside ``absorb`` rather than merely
    usually. The threshold is ``None``, so the first chunk spills and the second
    is the one appended — which is the window a running drain's teardown
    cancellation actually reaches once its stream has spilled.

    Nothing further is fed to the reader: the cancellation is delivered inside
    the held write, so the loop can only stop where ``absorb`` is awaited. A
    cancelled *read* is the other stop this drain already handles, and it is
    not what this case is reaching.
    """
    blocking = _BlockingSpillFile(tmp_path / "drain-cancelled", block_on=(1, 2))
    monkeypatch.setattr(
        "cuprum._capture_spill_io.NamedTemporaryFile",
        lambda **_: blocking,
    )
    first = "first chunk "
    second = "second chunk\n"
    config = _spill_config(tmp_path)

    async def run_case() -> CapturedDrain:
        """Spill the first chunk, then stop while the second is being written."""
        reader = asyncio.StreamReader()
        reader.feed_data(first.encode())
        task = asyncio.create_task(_drain(reader, config))
        # The spill's own write is the first, and is released immediately; the
        # append is the second, and is the write the case cancels inside.
        await _await_blocked_write(blocking, 1)
        blocking.release(1)
        reader.feed_data(second.encode())
        await _await_blocked_write(blocking, 2)
        task.cancel()
        blocking.release(2)
        drained = await task
        handle = drained.handle
        assert handle is not None, "a spilled drain must hand back its file handle"
        await handle.close(remove=True)
        return drained

    drained = asyncio.run(run_case())
    absorbed = len(first) + len(second)

    assert drained.capture is not None, (
        "a stopped spill must still report the capture it made; got None"
    )
    assert drained.capture.total_bytes == absorbed, (
        "the record must count the chunk the file already holds, not report a "
        f"lost reader; got {drained.capture.total_bytes} of {absorbed} bytes"
    )
    assert drained.capture.storage == "file", (
        f"the chunk must be reported as spilled; got {drained.capture.storage!r}"
    )
