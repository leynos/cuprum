"""What a file-backed capture puts in its file, and what it says about it.

A spill is the one capture whose bytes leave the parent heap, so the questions
here are about fidelity and about the metadata a caller needs to act on the
file: is everything the child wrote in there, is the path real and readable,
and does the record accurately say who is responsible for removing it.

The threshold is the policy's whole memory contract, so both of its sides are
pinned: a stream that stays under it must finalize from memory exactly as
``FullCapture`` would, and a stream that crosses it must have every byte in the
file. A capture that spilled late, or that lost its buffered prefix on the
transition, would still produce a file and still report ``storage`` of
``"file"``; only reading the file back shows either.
"""

from __future__ import annotations

import asyncio
import threading
import typing as typ

import pytest

from cuprum._capture_decode import _DecodeSettings
from cuprum._capture_file import _FileAccumulator
from cuprum.capture_events import CaptureEvent, CaptureEventCategory
from cuprum.capture_observation import observe_capture
from cuprum.capture_policy import CapturePolicyKind, FileBackedCapture

if typ.TYPE_CHECKING:
    import pathlib

    from cuprum._capture_types import CaptureFinal

_TEXT = _DecodeSettings(encoding="utf-8", errors="strict", capture_bytes=False)
_BYTES = _DecodeSettings(encoding="utf-8", errors="strict", capture_bytes=True)
# Long enough that a three-byte character is split by a one-byte chunk, so an
# accumulator that appended per chunk without preserving order would show.
_PAYLOAD = "中文 payload\n".encode()


def _spilling_to(
    directory: pathlib.Path,
    *,
    threshold: int,
    keep_file: bool = False,
) -> FileBackedCapture:
    """Return a file-backed policy spilling into *directory* at *threshold*.

    Returns
    -------
    FileBackedCapture
        The policy, ready to hand to :func:`_spill`.
    """
    return FileBackedCapture(
        spill_threshold_bytes=threshold,
        directory=directory,
        keep_file=keep_file,
    )


def _spill(
    payload: bytes,
    policy: FileBackedCapture,
    *,
    settings: _DecodeSettings = _TEXT,
    chunks: int = 1,
) -> CaptureFinal:
    """Feed *payload* through *policy*'s accumulator in *chunks* pieces.

    The policy is passed rather than its fields because the three of them are
    one value — the library already models them as
    :class:`~cuprum.capture_policy.FileBackedCapture`, and unpacking it here
    only to repack it below would put a second copy of that shape in the test.

    Returns
    -------
    CaptureFinal
        The accumulator's finalized output, holding the file still on disk
        because nothing here cleans it up. Callers that spilled drive the
        returned handle's cleanup through :func:`_cleanup`, which is why the
        handle travels out rather than staying here.
    """
    accumulator = _FileAccumulator(
        policy=policy,
        stream="stdout",
        settings=settings,
    )
    size = -(-len(payload) // chunks)

    async def spill() -> CaptureFinal:
        """Absorb the stream in order, then finalize it on the same loop."""
        for start in range(0, len(payload), size):
            await accumulator.absorb(payload[start : start + size])
        return await accumulator.finalize()

    return asyncio.run(spill())


def _cleanup(final: CaptureFinal) -> None:
    """Close *final*'s spill file, removing it unless its policy kept it.

    Every case below spilled into ``tmp_path``, whose removal would leave the
    descriptor to the interpreter's exit. Closing it here — through the same
    handle the production run would use — keeps the suite from accumulating
    open files and exercises the removal path on the way out.
    """
    handle = final.handle
    if handle is not None:
        asyncio.run(handle.close(remove=not handle.keep))


def test_a_spilled_capture_writes_the_whole_stream_to_its_file(
    tmp_path: pathlib.Path,
) -> None:
    """The file holds every byte, including the prefix buffered before the spill.

    The transition is where a spill loses data: the buffered prefix has to move
    to the file as well as every later chunk. A file missing the prefix would
    still exist, still be named by the record, and still hold the tail.
    """
    final = _spill(_PAYLOAD, _spilling_to(tmp_path, threshold=4))

    assert final.capture.storage == "file", (
        "crossing the threshold must move the stream into the file"
    )
    assert final.capture.path is not None, "a spilled capture must name its file"
    assert final.capture.path.read_bytes() == _PAYLOAD, (
        "the spill file must hold the child's whole stream; got "
        f"{final.capture.path.read_bytes()!r}"
    )
    assert final.capture.total_bytes == len(_PAYLOAD), (
        "the record must count every byte the child wrote"
    )
    assert final.capture.storage_failed is False, (
        "a spill that succeeded must not report a storage failure"
    )
    _cleanup(final)


def test_a_spilled_capture_reports_its_file_as_owned_by_the_run(
    tmp_path: pathlib.Path,
) -> None:
    """An owned file is one this run must remove, and the record says so."""
    final = _spill(_PAYLOAD, _spilling_to(tmp_path, threshold=4))

    assert final.capture.owned is True, (
        "a file the caller did not keep is this run's to remove"
    )
    assert final.handle is not None, "a spilled capture carries its handle out"
    assert final.handle.owned is True, (
        "the handle must agree with the record about who removes the file"
    )
    _cleanup(final)


def test_a_kept_spill_file_is_reported_unowned_but_still_written(
    tmp_path: pathlib.Path,
) -> None:
    """``keep_file`` changes ownership, not the bytes the file holds."""
    final = _spill(_PAYLOAD, _spilling_to(tmp_path, threshold=4, keep_file=True))

    assert final.capture.owned is False, (
        "a file the caller kept is handed over, not removed by the run"
    )
    assert final.handle is not None, "a spilled capture carries its handle out"
    assert final.handle.keep is True, (
        "the handle must remember that this file is the caller's"
    )
    assert final.capture.path is not None, "a spilled capture must name its file"
    assert final.capture.path.read_bytes() == _PAYLOAD, (
        "a kept file must still hold every byte the child wrote"
    )
    _cleanup(final)


def test_a_spilled_capture_reports_text_as_absent_and_the_file_instead(
    tmp_path: pathlib.Path,
) -> None:
    """Text mode says ``None``: the payload is the file, which the caller reads.

    Reporting the text as well would mean holding the whole stream on the heap,
    which is the memory the policy exists to avoid; reporting the file's path
    in the payload's place would put a ``Path`` where every caller expects text
    or bytes.
    """
    final = _spill(b"short", _spilling_to(tmp_path, threshold=1))

    assert final.payload is None, (
        f"a spilled text capture must not also retain the text; got {final.payload!r}"
    )
    assert final.capture.storage == "file", (
        "the record must send the caller to the file, not to the payload"
    )
    _cleanup(final)


def test_a_spilled_capture_in_byte_mode_returns_the_files_bytes(
    tmp_path: pathlib.Path,
) -> None:
    """Byte mode hands back the child's own bytes, read from the file."""
    payload = bytes(range(256))
    final = _spill(payload, _spilling_to(tmp_path, threshold=4), settings=_BYTES)

    assert isinstance(final.payload, bytes), (
        f"byte mode must return bytes; got {type(final.payload).__name__}"
    )
    assert final.payload == payload, "byte mode must return the child's own bytes"
    assert final.capture.storage == "file", (
        "byte mode still reports that the bytes came out of a file"
    )
    _cleanup(final)


def test_a_stream_within_its_threshold_stays_on_the_heap(
    tmp_path: pathlib.Path,
) -> None:
    """Under the threshold the capture is a full capture, file and all absent.

    The threshold is a memory decision, so a stream that never crosses it must
    be indistinguishable from a :class:`~cuprum.capture_policy.FullCapture`
    apart from the policy's name: complete text, no file, no handle.
    """
    final = _spill(_PAYLOAD, _spilling_to(tmp_path, threshold=len(_PAYLOAD)))

    assert final.payload == _PAYLOAD.decode(), (
        "a stream within its threshold must finalize as complete text"
    )
    assert final.capture.storage == "memory", (
        "a stream within its threshold must stay on the heap"
    )
    assert final.capture.path is None, "an in-memory capture names no file"
    assert final.handle is None, "there is no file for the caller to remove"
    assert final.capture.policy is CapturePolicyKind.FILE_BACKED, (
        "the policy that governed the stream is still the one it was given"
    )


def test_the_threshold_is_crossed_by_exceeding_it_not_by_reaching_it(
    tmp_path: pathlib.Path,
) -> None:
    """The boundary: one byte over spills, exactly the threshold does not.

    The two arms differ by a single byte and must disagree about where the
    bytes went, which is the only way to pin an off-by-one in either direction.
    """
    payload = b"eight by"  # exactly eight bytes
    at_threshold = _spill(payload, _spilling_to(tmp_path, threshold=len(payload)))
    over_threshold = _spill(payload, _spilling_to(tmp_path, threshold=len(payload) - 1))

    assert at_threshold.capture.storage == "memory", (
        "a stream exactly at the threshold has not outgrown it"
    )
    assert over_threshold.capture.storage == "file", (
        "a single byte past the threshold must spill"
    )
    _cleanup(at_threshold)
    _cleanup(over_threshold)


def test_an_empty_spill_directory_is_named_by_the_policy_not_the_default(
    tmp_path: pathlib.Path,
) -> None:
    """The file is created in the directory the policy chose."""
    chosen = tmp_path / "spills"
    chosen.mkdir()

    final = _spill(_PAYLOAD, _spilling_to(chosen, threshold=4))

    assert final.capture.path is not None, "a spilled capture must name its file"
    assert final.capture.path.parent == chosen, (
        f"the spill must land in the policy's directory; got {final.capture.path}"
    )
    _cleanup(final)


def test_a_spill_reports_its_transition_once_with_the_bytes_moved(
    tmp_path: pathlib.Path,
) -> None:
    """The transition emits exactly one event, naming the stream and the bytes.

    Once, because the accumulator spills once: a second event per append would
    make the channel unusable for counting, and a spill that fired again after
    a failure would report a move that did not happen.
    """
    recorder: list[CaptureEvent] = []
    with observe_capture(recorder.append):
        final = _spill(_PAYLOAD, _spilling_to(tmp_path, threshold=1))

    assert [event.category for event in recorder] == [
        CaptureEventCategory.SPILLED_TO_FILE
    ], f"the transition must be reported exactly once; got {recorder!r}"
    assert recorder[0].stream == "stdout", (
        f"the event must name the stream it moved; got {recorder[0].stream!r}"
    )
    assert recorder[0].spilled_bytes == len(_PAYLOAD), (
        "the event must count the bytes it moved; "
        f"got {recorder[0].spilled_bytes} of {len(_PAYLOAD)}"
    )
    _cleanup(final)


def test_a_spill_reports_the_bytes_written_so_far_not_the_whole_stream(
    tmp_path: pathlib.Path,
) -> None:
    """The count is a measurement of the move, not a prediction of the stream.

    The event is emitted at the transition, when the tail has not arrived. The
    stream arrives in two chunks and the threshold is crossed by the first, so
    the event must report that chunk while the record reports the whole — a
    count read from ``total_bytes`` at finalize time would answer the second
    question with the first one's field.
    """
    recorder: list[CaptureEvent] = []
    with observe_capture(recorder.append):
        final = _spill(_PAYLOAD, _spilling_to(tmp_path, threshold=1), chunks=2)
    # The helper divides the payload into two chunks, so the first one is what
    # the threshold was crossed by. Asserting against a computed chunk width
    # rather than a literal keeps this honest if the payload changes.
    first_chunk = -(-len(_PAYLOAD) // 2)

    assert first_chunk < len(_PAYLOAD), (
        "this case needs a tail that had not arrived when the spill happened"
    )
    assert recorder[0].spilled_bytes == first_chunk, (
        f"the event must report the prefix it moved; got {recorder[0].spilled_bytes}"
    )
    assert final.capture.total_bytes == len(_PAYLOAD), (
        "the record must still report the child's whole stream"
    )
    _cleanup(final)


def test_no_event_is_emitted_for_a_stream_that_never_spills(
    tmp_path: pathlib.Path,
) -> None:
    """A capture that stays on the heap reports nothing about storage."""
    recorder: list[CaptureEvent] = []
    with observe_capture(recorder.append):
        _spill(_PAYLOAD, _spilling_to(tmp_path, threshold=4096))

    assert not recorder, (
        f"a memory-backed capture must emit no storage event; got {recorder!r}"
    )


def test_a_spill_survives_a_chunk_partition_that_splits_characters(
    tmp_path: pathlib.Path,
) -> None:
    """The file holds the child's bytes, however the reads were divided.

    Spilling is a byte-level move, so a partition that cuts a character in half
    must not be visible in the file. Text mode never decodes the spilled
    stream, so this is the assertion that pins the file as a byte-for-byte
    record rather than a re-encoded rendering.
    """
    final = _spill(_PAYLOAD, _spilling_to(tmp_path, threshold=1), chunks=len(_PAYLOAD))

    assert final.capture.path is not None, "a spilled capture must name its file"
    assert final.capture.path.read_bytes() == _PAYLOAD, (
        "a partition that splits characters must not change the file's bytes; "
        f"got {final.capture.path.read_bytes()!r}"
    )
    _cleanup(final)


class _BlockingSpillFile:
    """A spill file whose write waits until the test lets it finish.

    Holds the spill's transition open between creating the file and the prefix
    reaching it, which is the window a cancellation can land in.
    """

    def __init__(self, path: pathlib.Path) -> None:
        """Create *path*, so the file exists before the write is entered."""
        self.name = str(path)
        self.started = False
        self._released = threading.Event()
        self._file = path.open("wb")

    def write(self, data: bytes) -> int:
        """Signal that the write is in flight, then wait to be released."""
        self.started = True
        self._released.wait(30)
        return self._file.write(data)

    def flush(self) -> None:
        """Flush the underlying file."""
        self._file.flush()

    def close(self) -> None:
        """Close the underlying file."""
        self._file.close()

    def release(self) -> None:
        """Let the blocked write finish."""
        self._released.set()


async def _await_started_write(blocking: _BlockingSpillFile) -> None:
    """Fail unless the spill begins writing its file before the deadline.

    The signal is set by a worker thread, which no asyncio primitive can
    observe, so polling is the coordination.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10.0
    while loop.time() < deadline:
        if blocking.started:
            return
        await asyncio.sleep(0.01)
    pytest.fail("the spill never began writing the file it created")


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
        self._released.wait(30)
        self.file = _BlockingSpillFile(self.path)
        return self.file

    def release(self) -> None:
        """Let the blocked create make the file."""
        self._released.set()


async def _await_started_create(create: _BlockingCreate) -> None:
    """Fail unless the worker enters the create before the deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10.0
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

    ``keep_file=True`` is deliberate, exactly as in the write-window case: the
    caller's exemption is for a file the run completed and handed over, not one
    a cancelled create left behind.
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

    ``keep_file=True`` is deliberate: the caller's exemption covers the file
    the run was asked to hand over, not one a cancelled spill never finished
    writing, so teardown still removes it.
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
        await _await_started_write(blocking)
        task.cancel()
        blocking.release()
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


@pytest.mark.parametrize("threshold", [1, 2, 8, 64])
def test_the_record_counts_the_stream_even_when_the_file_holds_it(
    threshold: int,
    tmp_path: pathlib.Path,
) -> None:
    """``total_bytes`` describes the child's output, not the file's accounting."""
    payload = b"x" * 128
    final = _spill(payload, _spilling_to(tmp_path, threshold=threshold))

    assert final.capture.total_bytes == len(payload), (
        f"the record must count the whole stream; got {final.capture.total_bytes}"
    )
    assert final.capture.dropped_bytes == 0, (
        "a spilled capture discards nothing; it moves the bytes"
    )
    _cleanup(final)
