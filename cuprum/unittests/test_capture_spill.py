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


def _spill(
    payload: bytes,
    *,
    threshold: int,
    directory: pathlib.Path,
    keep_file: bool = False,
    settings: _DecodeSettings = _TEXT,
    chunks: int = 1,
) -> CaptureFinal:
    """Feed *payload* through a file-backed accumulator in *chunks* pieces.

    Returns
    -------
    CaptureFinal
        The accumulator's finalized output, holding the file still on disk
        because nothing has cleaned it up.
    """
    accumulator = _FileAccumulator(
        policy=FileBackedCapture(
            spill_threshold_bytes=threshold,
            directory=directory,
            keep_file=keep_file,
        ),
        stream="stdout",
        settings=settings,
    )
    size = -(-len(payload) // chunks)
    for start in range(0, len(payload), size):
        accumulator.absorb(payload[start : start + size])
    return accumulator.finalize()


def test_a_spilled_capture_writes_the_whole_stream_to_its_file(
    tmp_path: pathlib.Path,
) -> None:
    """The file holds every byte, including the prefix buffered before the spill.

    The transition is where a spill loses data: the buffered prefix has to move
    to the file as well as every later chunk. A file missing the prefix would
    still exist, still be named by the record, and still hold the tail.
    """
    final = _spill(_PAYLOAD, threshold=4, directory=tmp_path)

    assert final.capture.storage == "file"
    assert final.capture.path is not None, "a spilled capture must name its file"
    assert final.capture.path.read_bytes() == _PAYLOAD, (
        "the spill file must hold the child's whole stream; got "
        f"{final.capture.path.read_bytes()!r}"
    )
    assert final.capture.total_bytes == len(_PAYLOAD)
    assert final.capture.storage_failed is False


def test_a_spilled_capture_reports_its_file_as_owned_by_the_run(
    tmp_path: pathlib.Path,
) -> None:
    """An owned file is one this run must remove, and the record says so."""
    final = _spill(_PAYLOAD, threshold=4, directory=tmp_path)

    assert final.capture.owned is True, (
        "a file the caller did not keep is this run's to remove"
    )
    assert final.handle is not None, "a spilled capture carries its handle out"
    assert final.handle.owned is True


def test_a_kept_spill_file_is_reported_unowned_but_still_written(
    tmp_path: pathlib.Path,
) -> None:
    """``keep_file`` changes ownership, not the bytes the file holds."""
    final = _spill(_PAYLOAD, threshold=4, directory=tmp_path, keep_file=True)

    assert final.capture.owned is False, (
        "a file the caller kept is handed over, not removed by the run"
    )
    assert final.handle is not None
    assert final.handle.keep is True
    assert final.capture.path is not None
    assert final.capture.path.read_bytes() == _PAYLOAD


def test_a_spilled_capture_reports_text_as_absent_and_the_file_instead(
    tmp_path: pathlib.Path,
) -> None:
    """Text mode says ``None``: the payload is the file, which the caller reads.

    Reporting the text as well would mean holding the whole stream on the heap,
    which is the memory the policy exists to avoid; reporting the file's path
    in the payload's place would put a ``Path`` where every caller expects text
    or bytes.
    """
    final = _spill(b"short", threshold=1, directory=tmp_path)

    assert final.payload is None, (
        f"a spilled text capture must not also retain the text; got {final.payload!r}"
    )
    assert final.capture.storage == "file"


def test_a_spilled_capture_in_byte_mode_returns_the_files_bytes(
    tmp_path: pathlib.Path,
) -> None:
    """Byte mode hands back the child's own bytes, read from the file."""
    payload = bytes(range(256))
    final = _spill(payload, threshold=4, directory=tmp_path, settings=_BYTES)

    assert isinstance(final.payload, bytes), (
        f"byte mode must return bytes; got {type(final.payload).__name__}"
    )
    assert final.payload == payload, "byte mode must return the child's own bytes"
    assert final.capture.storage == "file"


def test_a_stream_within_its_threshold_stays_on_the_heap(
    tmp_path: pathlib.Path,
) -> None:
    """Under the threshold the capture is a full capture, file and all absent.

    The threshold is a memory decision, so a stream that never crosses it must
    be indistinguishable from a :class:`~cuprum.capture_policy.FullCapture`
    apart from the policy's name: complete text, no file, no handle.
    """
    final = _spill(_PAYLOAD, threshold=len(_PAYLOAD), directory=tmp_path)

    assert final.payload == _PAYLOAD.decode(), (
        "a stream within its threshold must finalize as complete text"
    )
    assert final.capture.storage == "memory"
    assert final.capture.path is None
    assert final.handle is None
    assert final.capture.policy is CapturePolicyKind.FILE_BACKED


def test_the_threshold_is_crossed_by_exceeding_it_not_by_reaching_it(
    tmp_path: pathlib.Path,
) -> None:
    """The boundary: one byte over spills, exactly the threshold does not.

    The two arms differ by a single byte and must disagree about where the
    bytes went, which is the only way to pin an off-by-one in either direction.
    """
    payload = b"eight by"  # exactly eight bytes
    at_threshold = _spill(payload, threshold=len(payload), directory=tmp_path)
    over_threshold = _spill(payload, threshold=len(payload) - 1, directory=tmp_path)

    assert at_threshold.capture.storage == "memory"
    assert over_threshold.capture.storage == "file"


def test_an_empty_spill_directory_is_named_by_the_policy_not_the_default(
    tmp_path: pathlib.Path,
) -> None:
    """The file is created in the directory the policy chose."""
    chosen = tmp_path / "spills"
    chosen.mkdir()

    final = _spill(_PAYLOAD, threshold=4, directory=chosen)

    assert final.capture.path is not None
    assert final.capture.path.parent == chosen, (
        f"the spill must land in the policy's directory; got {final.capture.path}"
    )


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
        _spill(_PAYLOAD, threshold=1, directory=tmp_path)

    assert [event.category for event in recorder] == [
        CaptureEventCategory.SPILLED_TO_FILE
    ]
    assert recorder[0].stream == "stdout"
    assert recorder[0].spilled_bytes == len(_PAYLOAD), (
        "the event must count the bytes it moved; "
        f"got {recorder[0].spilled_bytes} of {len(_PAYLOAD)}"
    )


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
        final = _spill(_PAYLOAD, threshold=1, directory=tmp_path, chunks=2)
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


def test_no_event_is_emitted_for_a_stream_that_never_spills(
    tmp_path: pathlib.Path,
) -> None:
    """A capture that stays on the heap reports nothing about storage."""
    recorder: list[CaptureEvent] = []
    with observe_capture(recorder.append):
        _spill(_PAYLOAD, threshold=4096, directory=tmp_path)

    assert recorder == [], (
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
    final = _spill(_PAYLOAD, threshold=1, directory=tmp_path, chunks=len(_PAYLOAD))

    assert final.capture.path is not None
    assert final.capture.path.read_bytes() == _PAYLOAD


@pytest.mark.parametrize("threshold", [1, 2, 8, 64])
def test_the_record_counts_the_stream_even_when_the_file_holds_it(
    threshold: int,
    tmp_path: pathlib.Path,
) -> None:
    """``total_bytes`` describes the child's output, not the file's accounting."""
    payload = b"x" * 128
    final = _spill(payload, threshold=threshold, directory=tmp_path)

    assert final.capture.total_bytes == len(payload), (
        f"the record must count the whole stream; got {final.capture.total_bytes}"
    )
    assert final.capture.dropped_bytes == 0, (
        "a spilled capture discards nothing; it moves the bytes"
    )
