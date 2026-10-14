"""What each accumulator retains, and what it reports retaining.

The accumulators are the whole of the retention policy: the drain hands each
one the child's chunks and reads back a payload and a record. These tests drive
them directly rather than through a run, because the questions here are about
retention arithmetic — how many bytes were dropped, whether the retained tail
still decodes, whether a full capture is unchanged — and a real child would
make every one of those answers depend on how the pipe happened to partition
the stream.

``FullCapture`` is checked against the semantics it replaced: the accumulator
must return exactly what the old ``bytearray``-then-``decode`` path returned,
byte for byte, so a caller who configured nothing cannot see a difference.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum._capture_accumulators import (
    _BoundedAccumulator,
    _FullAccumulator,
    build_accumulator,
)
from cuprum._capture_decode import _DecodeSettings
from cuprum.capture_policy import (
    BoundedCapture,
    CapturePolicyKind,
    FileBackedCapture,
    FullCapture,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._capture_types import CaptureSink
    from cuprum.capture_policy import CapturedStream, CaptureStreamName

_TEXT = _DecodeSettings(encoding="utf-8", errors="strict", capture_bytes=False)
# The same decode, but the child's own bytes are the payload. Both modes are
# exercised by the same cases, so a mode that quietly decoded — or a text mode
# that quietly returned bytes — cannot pass by running only its own arm.
_BYTES = _DecodeSettings(encoding="utf-8", errors="strict", capture_bytes=True)

# Corpora chosen for what a retention bug would have to do to pass them. The
# multibyte and split-code-point entries are the ones a length-based trim gets
# wrong; the empty entry is the one that catches a "both empty" degeneracy.
_CORPUS: tuple[bytes, ...] = (
    b"",
    b"a",
    b"plain ascii payload",
    "café 中文 \U0001f600".encode(),
    # One code point's bytes, so a bound landing mid-sequence lands inside the
    # only character there is.
    "\U0001f600".encode(),
    bytes(range(0x20, 0x7F)),
)


def _sink(
    policy: FullCapture | BoundedCapture,
    *,
    stream: CaptureStreamName = "stdout",
    settings: _DecodeSettings = _TEXT,
) -> CaptureSink:
    """Build the accumulator for *policy*, through the drain's own entry point."""
    sink = build_accumulator(policy, stream=stream, settings=settings)
    assert sink is not None, "a named policy must build an accumulator"
    return sink


def _absorb(sink: CaptureSink, chunks: cabc.Iterable[bytes]) -> None:
    """Feed each chunk to *sink*, in order."""
    for chunk in chunks:
        sink.absorb(chunk)


@pytest.mark.parametrize("payload", _CORPUS)
@pytest.mark.parametrize("chunk_size", [1, 3, 64])
def test_full_capture_reproduces_the_stream_it_replaced(
    payload: bytes,
    chunk_size: int,
) -> None:
    """A full capture returns exactly what the old bytearray path returned.

    The reference is written out rather than imported, because the point is
    that the accumulator still agrees with the semantics a caller had before
    policies existed: buffer the chunks, decode once at the end. A shared
    helper would agree with itself.

    The chunk sizes matter independently of the payload: 1 splits every
    multibyte code point, and 3 splits some of them while fitting others
    whole, so an accumulator that decoded per chunk rather than once at the
    end fails here rather than in a run that happened to read whole lines.
    """
    expected = payload.decode("utf-8", errors="strict")
    sink = _sink(FullCapture())

    _absorb(sink, _chunks(payload, chunk_size))
    final = sink.finalize()

    assert final.payload == expected, (
        f"a full capture must return the decoded stream; got {final.payload!r}"
    )
    assert final.capture.policy is CapturePolicyKind.FULL
    assert final.capture.total_bytes == len(payload)
    assert final.capture.dropped_bytes == 0, "a full capture drops nothing"
    assert final.capture.storage == "memory"
    assert final.handle is None, "a full capture owns no file"


@pytest.mark.parametrize("payload", _CORPUS)
@pytest.mark.parametrize("chunk_size", [1, 3, 64])
def test_full_capture_in_bytes_mode_returns_the_original_bytes(
    payload: bytes,
    chunk_size: int,
) -> None:
    """Byte mode hands back the stream untouched, whatever the partition."""
    sink = _sink(FullCapture(), settings=_BYTES)

    _absorb(sink, _chunks(payload, chunk_size))

    assert sink.finalize().payload == payload


@pytest.mark.parametrize("payload", _CORPUS)
@pytest.mark.parametrize("max_bytes", [1, 2, 3, 4, 5, 8, 64, 4096])
def test_bounded_capture_retains_a_suffix_within_its_bound(
    payload: bytes,
    max_bytes: int,
) -> None:
    """The retained tail is a suffix of the stream, and never over the bound.

    Every bound from 1 to 4096 is exercised over a corpus whose multibyte
    entries are longer than several of those bounds, so the trim has real work
    to do rather than passing on payloads that fit whole. The bound is
    checked on the *retained bytes*, read back from the record's own counts,
    because a payload compared as text would hide a byte-level overrun behind
    the trim's forward movement.
    """
    sink = _sink(BoundedCapture(max_bytes=max_bytes))

    _absorb(sink, _chunks(payload, 1))
    final = sink.finalize()
    record = final.capture

    assert record.total_bytes == len(payload), (
        "the record must count the whole stream, not just the retained tail"
    )
    assert record.dropped_bytes + _retained_len(record) == len(payload), (
        "the lemma dropped + retained == total must hold; "
        f"got dropped={record.dropped_bytes}, total={record.total_bytes}"
    )
    assert _retained_len(record) <= max_bytes, (
        f"retention must stay within the bound; got {_retained_len(record)} "
        f"bytes for max_bytes={max_bytes}"
    )
    # The retained bytes are a suffix: dropping the first ``dropped`` bytes of
    # the stream has to leave exactly what the payload decodes to. A trim that
    # took bytes off the wrong end would fail here even while keeping its
    # length, which the two assertions above cannot see.
    assert final.payload == payload[record.dropped_bytes :].decode(
        "utf-8", errors="strict"
    ), f"the retained tail must be the stream's suffix; got {final.payload!r}"


@pytest.mark.parametrize("max_bytes", [1, 2, 3, 4, 8, 4096])
def test_bounded_capture_reports_a_truncation_only_when_it_dropped(
    max_bytes: int,
) -> None:
    """A stream that fits the bound is not a truncated capture.

    The bound is the only thing that decides this, so the same payload is run
    twice: once at a bound it fits, where nothing may be reported, and once at
    the parametrized bound, where the report must follow the drop count rather
    than the policy's kind. Both streams carry a ``BoundedCapture``, so a
    ``truncated`` that merely reflected the policy class could not tell the two
    arms apart.
    """
    payload = b"twelve bytes"
    fits = _sink(BoundedCapture(max_bytes=len(payload)))
    over = _sink(BoundedCapture(max_bytes=max_bytes))

    _absorb(fits, [payload])
    _absorb(over, [payload])

    assert fits.finalize().capture.truncated is False, (
        "a stream exactly as long as the bound drops nothing and is whole"
    )
    assert over.finalize().capture.truncated is (max_bytes < len(payload)), (
        "truncation must follow the drop count; "
        f"got {over.finalize().capture.dropped_bytes} dropped bytes "
        f"for max_bytes={max_bytes}"
    )


@pytest.mark.parametrize("max_bytes", [1, 2, 3, 5, 7])
def test_bounded_capture_never_splits_a_code_point(max_bytes: int) -> None:
    """A strict decoder reads the retained tail without raising.

    The tail is cut at an arbitrary byte offset, so it can begin inside a
    multi-byte character. Decoding is where that would surface — as a
    ``UnicodeDecodeError`` under the run's strict policy, or as a leading
    replacement character under a lenient one — and this asserts the first
    cannot happen for any bound.

    The payload is nothing but four-byte code points, so *every* bound below
    its length lands mid-character and the trim has to move every time.
    """
    payload = "\U0001f600\U0001f601\U0001f602".encode()
    sink = _sink(BoundedCapture(max_bytes=max_bytes))

    _absorb(sink, [payload])
    final = sink.finalize()

    assert isinstance(final.payload, str), "text mode must decode to text"
    assert "�" not in final.payload, (
        "a trimmed tail must not grow a replacement character at its leading "
        f"edge; got {final.payload!r}"
    )
    assert payload.endswith(final.payload.encode()), (
        f"the decoded tail must be the stream's suffix; got {final.payload!r}"
    )


@pytest.mark.parametrize("max_bytes", [1, 2, 3, 6])
def test_bounded_capture_in_bytes_mode_is_never_longer_than_the_bound(
    max_bytes: int,
) -> None:
    """Byte mode is trimmed too, so the bound holds in the child's own bytes."""
    payload = "\U0001f600\U0001f601\U0001f602".encode()
    sink = _sink(BoundedCapture(max_bytes=max_bytes), settings=_BYTES)

    _absorb(sink, [payload])
    final = sink.finalize()

    assert isinstance(final.payload, bytes), "byte mode must return bytes"
    assert len(final.payload) <= max_bytes, (
        f"byte mode must not exceed the bound; got {len(final.payload)} bytes"
    )
    assert payload.endswith(final.payload), (
        f"byte mode must retain a suffix; got {final.payload!r}"
    )


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 4096])
def test_bounded_capture_drops_from_the_front_not_the_back(
    chunk_size: int,
) -> None:
    """The retained bytes are the *most recent* ones, however they arrived.

    A bound of ``len(payload) - 1`` is the sharpest case: exactly one byte must
    be dropped, so an accumulator that dropped a whole chunk, or dropped from
    the tail instead of the head, is visible in the payload itself rather than
    in a count.
    """
    payload = b"abcdefghij"
    sink = _sink(BoundedCapture(max_bytes=len(payload) - 1))

    _absorb(sink, _chunks(payload, chunk_size))
    final = sink.finalize()

    assert final.payload == payload[1:].decode(), (
        f"the tail must be kept and the head dropped; got {final.payload!r}"
    )
    assert final.capture.dropped_bytes == 1


def test_build_accumulator_returns_nothing_for_an_uncaptured_stream() -> None:
    """No policy means no accumulator, which is how a discarded stream is said."""
    assert build_accumulator(None, stream="stdout", settings=_TEXT) is None


def test_build_accumulator_selects_the_file_backed_sink() -> None:
    """A file-backed policy reaches its own accumulator through the shared seam."""
    sink = build_accumulator(
        FileBackedCapture(),
        stream="stderr",
        settings=_TEXT,
    )

    assert sink is not None, "a file-backed policy must build an accumulator"
    assert sink.finalize().capture.policy is CapturePolicyKind.FILE_BACKED


def test_build_accumulator_rejects_a_value_that_is_not_a_policy() -> None:
    """A non-policy reaches the ``match`` fallthrough rather than a default.

    The parameter is typed, so only a caller that bypassed the type checker can
    get here; the test bypasses it deliberately, because the alternative to
    this error is a silently wrong accumulator.
    """
    with pytest.raises(TypeError, match="not a capture policy"):
        build_accumulator(
            typ.cast("typ.Any", "full"),
            stream="stdout",
            settings=_TEXT,
        )


def test_the_two_accumulators_are_distinguishable_by_type() -> None:
    """Each policy builds the class that implements it, not a shared one."""
    assert isinstance(_sink(FullCapture()), _FullAccumulator)
    assert isinstance(_sink(BoundedCapture(max_bytes=8)), _BoundedAccumulator)


def _chunks(payload: bytes, size: int) -> list[bytes]:
    """Split *payload* into consecutive chunks of at most *size* bytes.

    Returns
    -------
    list[bytes]
        The chunks, in stream order, whose concatenation is *payload*.
    """
    return [payload[start : start + size] for start in range(0, len(payload), size)]


def _retained_len(record: CapturedStream) -> int:
    """Report how many bytes the record's policy retained, from its own counts."""
    return record.total_bytes - record.dropped_bytes
