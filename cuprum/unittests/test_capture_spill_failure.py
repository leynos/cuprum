"""What a file-backed capture does when its storage refuses to cooperate.

The contract is that a failure to *retain* output is never a failure of the
command that produced it. The child still runs to completion, the caller still
receives a result, and the capture reports what it can. That makes these the
tests of an *absorbed* failure rather than of a raised one: each case asserts
the capture completed, then what the caller can still read, and only then that
the failure was reported at all.

The two failures are deliberately different. A failed *create* leaves the
stream on the heap, so the caller reads complete text. A failed *append* leaves
a file holding the prefix that reached it before the failure, so the record has
to say the capture is incomplete — otherwise a caller would read a short file
and believe it was the whole stream.

Every case asserts the exception class *name* reaches the event and that the
exception's message does not: the channel carries low-cardinality values, and a
message could hold a path or a payload.
"""

from __future__ import annotations

import asyncio
import errno
import pathlib
import typing as typ

from cuprum._capture_decode import _DecodeSettings
from cuprum._capture_file import _FileAccumulator
from cuprum.capture_events import CaptureEvent, CaptureEventCategory
from cuprum.capture_observation import observe_capture
from cuprum.capture_policy import CaptureStreamName, FileBackedCapture
from cuprum.unittests._capture_sink_support import (
    _absorb,
    _drain_sink,
    _finalize,
)

if typ.TYPE_CHECKING:
    import io

    import pytest

    from cuprum._capture_types import CaptureFinal

_TEXT = _DecodeSettings(encoding="utf-8", errors="strict", capture_bytes=False)
_PAYLOAD = b"the child's output, in full\n"
_PREFIX = b"buffered prefix "
_TAIL = b"tail"
_FULL = _PREFIX + _TAIL
# A message distinctive enough that any test leaking it into an event field
# fails rather than passing on a plausible-looking string.
_LEAKY_MESSAGE = "secret-path-/tmp/do-not-forward"


class _SpillFileDouble:
    """A spill file backed by a real path, whose writes and flushes can fail.

    The accumulator reads the file back in byte mode and unlinks it on a failed
    transition, so a double that answered without touching the filesystem
    would make half of these cases untestable. This one holds a real descriptor
    and reports what reached the file, discarding nothing silently.
    """

    def __init__(
        self,
        path: pathlib.Path,
        error: OSError,
        *,
        fail_write: bool = True,
        fail_flush: bool = False,
    ) -> None:
        """Bind the backing path and which of the two calls fails."""
        self.name = str(path)
        self.path = path
        self.error = error
        self.fail_write = fail_write
        self.fail_flush = fail_flush
        self.writes = 0
        self.closed = False
        self._file: io.BufferedWriter = path.open("wb")

    def write(self, data: bytes) -> int:
        """Write through to the file, or fail as configured."""
        self.writes += 1
        if self.fail_write:
            raise self.error
        return self._file.write(data)

    def flush(self) -> None:
        """Flush to the file, or fail as configured."""
        if self.fail_flush:
            raise self.error
        self._file.flush()

    def close(self) -> None:
        """Close the backing file once."""
        if not self.closed:
            self.closed = True
            self._file.close()


class _SpillFiles:
    """Every spill file one test creates, so a case can inspect them.

    Holds the configuration for each new file and the files themselves, because
    a case has to reach the object the accumulator wrote through in order to
    flip a failure on partway through a stream.
    """

    def __init__(
        self,
        error: OSError,
        *,
        fail_write: bool = True,
        fail_flush: bool = False,
    ) -> None:
        """Bind the failure every file this factory hands out will report."""
        self.error = error
        self.fail_write = fail_write
        self.fail_flush = fail_flush
        self.created: list[_SpillFileDouble] = []

    def factory(
        self,
        *,
        delete: bool,
        dir: str | pathlib.Path | None,  # ruff: ignore[builtin-argument-shadowing] - the stdlib keyword
    ) -> _SpillFileDouble:
        """Create one double, at a real path inside the requested directory."""
        assert delete is False, "the accumulator must ask for a surviving file"
        base = pathlib.Path(dir) if dir is not None else pathlib.Path.cwd()
        file = _SpillFileDouble(
            base / f"spill-{len(self.created)}",
            self.error,
            fail_write=self.fail_write,
            fail_flush=self.fail_flush,
        )
        self.created.append(file)
        return file

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch the spill create to hand out files this factory configures."""
        monkeypatch.setattr(
            "cuprum._capture_spill_io.NamedTemporaryFile",
            self.factory,
        )

    @property
    def only(self) -> _SpillFileDouble:
        """The single file this test created."""
        assert len(self.created) == 1, (
            f"expected one spill file, got {len(self.created)}"
        )
        return self.created[0]


class _RefusingCreate:
    """A ``NamedTemporaryFile`` that never makes a file, failing *error*."""

    def __init__(self, error: OSError) -> None:
        """Bind the failure and start with no attempts recorded."""
        self.error = error
        self.attempts = 0

    def __call__(self, **_: object) -> typ.NoReturn:
        """Count the attempt, then fail it."""
        self.attempts += 1
        raise self.error

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch the spill create to refuse every attempt."""
        monkeypatch.setattr(
            "cuprum._capture_spill_io.NamedTemporaryFile",
            self,
        )


def _accumulator(
    *,
    directory: pathlib.Path,
    threshold: int = 1,
    stream: CaptureStreamName = "stderr",
) -> _FileAccumulator:
    """Build a file-backed accumulator that spills into *directory*."""
    return _FileAccumulator(
        policy=FileBackedCapture(spill_threshold_bytes=threshold, directory=directory),
        stream=stream,
        settings=_TEXT,
    )


def _cleanup(accumulator: _FileAccumulator) -> None:
    """Run the accumulator's async cleanup to completion."""
    asyncio.run(accumulator.cleanup())


def test_a_failed_create_keeps_the_stream_in_memory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create that fails leaves the capture complete, merely on the heap.

    Nothing was written anywhere else, so buffering continues and the caller
    reads the child's whole output as text. The record reports ``"memory"``
    beside ``storage_failed``: the storage the policy asked for is not the
    storage it got, and saying so is what lets a caller notice.
    """
    create = _RefusingCreate(OSError(errno.ENOSPC, "No space left on device"))
    create.install(monkeypatch)
    accumulator = _accumulator(directory=tmp_path)

    final = _drain_sink(accumulator, [_PAYLOAD])

    assert final.payload == _PAYLOAD.decode(), (
        "a failed spill must not lose the stream; the caller still reads it"
    )
    assert final.capture.storage == "memory", (
        "a failed create leaves the capture on the heap, with no file to name"
    )
    assert final.capture.storage_failed is True, (
        "the refusal must be visible on the record, not only in the event"
    )
    assert final.capture.total_bytes == len(_PAYLOAD), (
        "the record counts every byte the child wrote, not only what was kept"
    )
    assert final.handle is None, "there is no file to hand over"


def test_a_failed_create_reports_the_error_class_and_not_its_message(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The event names the exception class and carries nothing else from it."""
    _RefusingCreate(
        OSError(errno.EACCES, _LEAKY_MESSAGE),
    ).install(monkeypatch)
    recorder: list[CaptureEvent] = []

    with observe_capture(recorder.append):
        _absorb(_accumulator(directory=tmp_path), [_PAYLOAD])

    assert [event.category for event in recorder] == [
        CaptureEventCategory.STORAGE_WRITE_FAILED
    ], f"the refused create must be reported once; got {recorder!r}"
    event = recorder[0]
    # The concrete class, not ``OSError``: ``errno.EACCES`` constructs a
    # ``PermissionError``, and naming the base would hide which failure it was.
    assert event.error_type == "PermissionError", (
        f"the event must name the error's own class; got {event.error_type!r}"
    )
    assert _LEAKY_MESSAGE not in repr(event), (
        f"the event must not carry the exception's message; got {event!r}"
    )
    assert event.stream == "stderr", (
        f"the event must name its stream; got {event.stream!r}"
    )


def test_a_failed_create_is_reported_once_however_the_stream_grows(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused spill is attempted once, and the stream stays on the heap.

    Retrying would cost one syscall and one ``STORAGE_WRITE_FAILED`` event per
    chunk that crosses the threshold, so a full or read-only directory would
    flood a metrics channel with a failure that has already been reported and
    cannot succeed. The buffer keeps growing either way — the bytes stay on the
    heap, which is what the create-failure recovery is for — but storage is not
    asked again.

    Pinned as an exact count so a future change that resumed re-offering, or
    that stopped reporting the single refusal, has to be argued here rather
    than absorbed silently.
    """
    create = _RefusingCreate(OSError(errno.ENOSPC, "No space left on device"))
    create.install(monkeypatch)
    recorder: list[CaptureEvent] = []

    with observe_capture(recorder.append):
        accumulator = _accumulator(directory=tmp_path)
        final = _drain_sink(accumulator, [b"chunk"] * 10)

    assert create.attempts == 1, (
        f"a refused create must not be retried per chunk; got {create.attempts}"
    )
    assert [event.category for event in recorder] == [
        CaptureEventCategory.STORAGE_WRITE_FAILED
    ], f"the refusal must be reported exactly once; got {len(recorder)} events"
    assert recorder[0].error_type == "OSError", (
        f"the event names the error class; got {recorder[0].error_type!r}"
    )
    assert final.capture.storage_failed is True, (
        "the single refusal must still be visible on the record"
    )
    assert final.payload == "chunk" * 10, (
        "every chunk must still reach the caller after the failure"
    )


def test_a_failed_first_write_removes_the_half_made_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file that never took the prefix is removed rather than orphaned.

    This is not the append path: the prefix is still in hand and the file never
    became the capture, so the accumulator abandons it entirely — the stream
    stays on the heap, the descriptor is closed, and the file is unlinked.
    """
    files = _SpillFiles(OSError(errno.EIO, "Input/output error"))
    files.install(monkeypatch)
    accumulator = _accumulator(directory=tmp_path)

    final = _drain_sink(accumulator, [_PAYLOAD])

    assert files.only.closed is True, "the abandoned file's descriptor must close"
    assert not files.only.path.exists(), (
        "a file that never held the capture must not be left behind"
    )
    assert final.capture.storage == "memory", (
        "a create that failed leaves the capture on the heap"
    )
    assert final.capture.storage_failed is True, (
        "the failed transition must be reported on the record"
    )
    assert final.payload == _PAYLOAD.decode(), (
        "the failed transition must not lose the stream"
    )


def test_a_failed_append_keeps_the_prefix_that_reached_the_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later write failure stops the spill and keeps what arrived.

    This is where the record's ``storage_failed`` is load-bearing: the file
    exists, is named by the record, and holds only part of the stream. A caller
    that read it without checking would take a truncated capture for a complete
    one.
    """
    files = _SpillFiles(OSError(errno.ENOSPC, "No space left on device"))
    # The spill itself must succeed, so the refusal is held back for the append.
    files.fail_write = False
    files.install(monkeypatch)
    accumulator = _accumulator(directory=tmp_path)

    async def append_across_the_failure() -> CaptureFinal:
        """Spill the prefix, arm the refusal, then append the tail."""
        await accumulator.absorb(_PREFIX)
        files.only.fail_write = True
        await accumulator.absorb(_TAIL)
        return await accumulator.finalize()

    final = asyncio.run(append_across_the_failure())

    assert final.capture.storage == "file", "the spill succeeded, so the file stands"
    assert final.capture.storage_failed is True, (
        "a stopped append must report the capture as incomplete"
    )
    assert final.capture.path is not None, (
        "the failed append must still name the file that exists"
    )
    assert final.capture.path.read_bytes() == _PREFIX, (
        "the file must hold exactly what reached it before the failure; "
        f"got {final.capture.path.read_bytes()!r}"
    )
    assert final.capture.total_bytes == len(_FULL), (
        "the record must still count what the child wrote, not what was kept"
    )
    _cleanup(accumulator)


def test_a_failed_append_stops_writing_rather_than_retrying_per_chunk(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a refused append, later chunks are not offered to the file again."""
    files = _SpillFiles(OSError(errno.ENOSPC, "No space left on device"))
    files.fail_write = False
    files.install(monkeypatch)
    recorder: list[CaptureEvent] = []

    with observe_capture(recorder.append):
        accumulator = _accumulator(directory=tmp_path)

        async def stop_after_the_refusal() -> None:
            """Spill the prefix, arm the refusal, then offer five more chunks."""
            await accumulator.absorb(_PREFIX)
            files.only.fail_write = True
            for _ in range(5):
                await accumulator.absorb(b"later chunk")

        asyncio.run(stop_after_the_refusal())

    # One write took the prefix, one was refused; the five chunks after that
    # refusal must not have reached the file at all.
    assert files.only.writes == 2, (
        "a stopped append must not be retried per chunk; "
        f"got {files.only.writes} writes"
    )
    assert [event.category for event in recorder] == [
        CaptureEventCategory.SPILLED_TO_FILE,
        CaptureEventCategory.STORAGE_WRITE_FAILED,
    ], f"the refusal must be reported once, after the spill; got {recorder!r}"
    _cleanup(accumulator)


def test_a_failed_append_flush_is_reported_as_a_flush_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two storage failures are distinguishable in the channel.

    Which call failed decides what a caller could do about it, so collapsing
    them into one category would discard the only diagnostic the channel
    carries. The *append* path is where this matters: a failed flush during the
    spill transition abandons the file and keeps the stream in memory (tested
    below), while a failed flush after the transition stops the file where it
    is and marks the capture incomplete.
    """
    files = _SpillFiles(OSError(errno.ENOSPC, "No space left on device"))
    files.fail_write = False
    files.install(monkeypatch)
    recorder: list[CaptureEvent] = []

    with observe_capture(recorder.append):
        accumulator = _accumulator(directory=tmp_path)

        async def flush_across_the_failure() -> CaptureFinal:
            """Spill the prefix, arm the refusal, then append and finalize."""
            await accumulator.absorb(_PREFIX)
            files.only.fail_flush = True
            await accumulator.absorb(_TAIL)
            return await accumulator.finalize()

        final = asyncio.run(flush_across_the_failure())

    assert [event.category for event in recorder] == [
        CaptureEventCategory.SPILLED_TO_FILE,
        CaptureEventCategory.STORAGE_FLUSH_FAILED,
    ], f"a failed flush must report itself as one; got {recorder!r}"
    assert recorder[1].error_type == "OSError", (
        f"the flush event names the error class; got {recorder[1].error_type!r}"
    )
    assert recorder[1].spilled_bytes == len(_PREFIX), (
        "the event must report the bytes that did reach the file; "
        f"got {recorder[1].spilled_bytes}"
    )
    assert final.capture.storage == "file", (
        "the failed flush keeps the file-backed record"
    )
    assert final.capture.storage_failed is True, (
        "a refused flush must mark the capture incomplete"
    )
    assert final.capture.path is not None, "the record must still name the file"
    assert final.capture.path.read_bytes() == _PREFIX, (
        "a refused flush must leave the file holding the prefix that arrived"
    )
    _cleanup(accumulator)


def test_a_failed_flush_during_the_spill_abandons_the_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flush that fails while moving the prefix is a failed transition.

    The prefix is still in hand at that moment, so the stream stays on the heap
    and the file is removed — the same recovery as a refused write, reached
    through the other call.
    """
    files = _SpillFiles(
        OSError(errno.ENOSPC, "No space left on device"),
        fail_write=False,
        fail_flush=True,
    )
    files.install(monkeypatch)
    accumulator = _accumulator(directory=tmp_path)

    final = _drain_sink(accumulator, [_PAYLOAD])

    assert final.capture.storage == "memory", (
        "an abandoned spill leaves the capture on the heap"
    )
    assert final.capture.storage_failed is True, (
        "the abandoned spill must be reported on the record"
    )
    assert final.payload == _PAYLOAD.decode(), (
        "the prefix must still reach the caller after a failed transition"
    )
    assert not files.only.path.exists(), (
        "a file whose flush failed must not be left behind"
    )


def test_cleanup_after_a_failed_create_leaves_no_file_and_does_not_raise(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleaning up a failed spill is a safe no-op, however often it runs.

    Teardown reaches this sink whatever happened during the run, and the normal
    path may reach it too, so a failure to create the file must leave cleanup
    with nothing to do rather than with an error. Running it twice is what
    witnesses the idempotence.
    """
    _RefusingCreate(OSError(errno.ENOSPC, "No space left on device")).install(
        monkeypatch
    )
    accumulator = _accumulator(directory=tmp_path)

    _absorb(accumulator, [_PAYLOAD])
    _cleanup(accumulator)
    _cleanup(accumulator)

    assert not list(tmp_path.iterdir()), (
        "a failed spill must leave nothing in the spill directory"
    )


def test_a_vanished_spill_file_is_absorbed_when_byte_mode_reads_it_back(
    tmp_path: pathlib.Path,
) -> None:
    """A spill that disappears before finalization does not fail the run.

    ``keep_file=True`` transfers the file to the caller, who may read and then
    remove it before the run materializes its result. Byte mode reads the file
    back at that point, so the read is a storage call like any other and has to
    be absorbed the way a failed create or append is: a failure to *retain*
    output is not a failure of the command that produced it. The record reports
    ``storage_failed`` because the capture is no longer available in full, and
    the read failure reaches the event channel like any other storage failure.
    """
    settings = _DecodeSettings(encoding="utf-8", errors="strict", capture_bytes=True)
    accumulator = _FileAccumulator(
        policy=FileBackedCapture(
            spill_threshold_bytes=1, directory=tmp_path, keep_file=True
        ),
        stream="stdout",
        settings=settings,
    )
    events: list[CaptureEvent] = []
    _absorb(accumulator, [_PAYLOAD])
    entries = list(tmp_path.iterdir())
    assert len(entries) == 1, (
        f"the capture had to spill before it could vanish; found {entries}"
    )
    (path,) = entries
    path.unlink()

    with observe_capture(events.append):
        final = _finalize(accumulator)

    assert final.payload is None, (
        "a spill whose bytes cannot be read is not a payload the caller can use"
    )
    assert final.capture.storage == "file", (
        "a failed read-back keeps the file-backed record"
    )
    assert final.capture.storage_failed is True, (
        "the record must say the capture is incomplete rather than merely short"
    )
    assert [event.category for event in events] == [
        CaptureEventCategory.STORAGE_READ_FAILED
    ], "the read failure must reach the event channel"
    assert events[0].error_type == "FileNotFoundError", (
        "the event names the error class, never its message"
    )
    _cleanup(accumulator)


def test_a_vanished_spill_file_is_still_reported_as_text_in_text_mode(
    tmp_path: pathlib.Path,
) -> None:
    """A vanished spill leaves text mode with its path, not an error.

    Text mode never reads the file back, so a file that disappears between the
    spill and finalization does not change what the caller reads: the record
    still names the path and the capture is reported as spilled. This is the
    control for the byte-mode case beside it, which has a read to absorb.
    """
    accumulator = _accumulator(directory=tmp_path)
    _absorb(accumulator, [_PAYLOAD])
    entries = list(tmp_path.iterdir())
    assert len(entries) == 1, (
        f"the capture had to spill before it could vanish; found {entries}"
    )
    (path,) = entries
    path.unlink()

    final = _finalize(accumulator)

    assert final.payload is None, (
        "text mode reports the spill as a path rather than reading its bytes"
    )
    assert final.capture.storage == "file", (
        "a completed spill keeps the file-backed record"
    )
    assert final.capture.path == path, (
        f"the record must name the spill file; got {final.capture.path!r}"
    )
    assert final.capture.storage_failed is False, (
        "nothing the text-mode capture asked for failed"
    )
    _cleanup(accumulator)
