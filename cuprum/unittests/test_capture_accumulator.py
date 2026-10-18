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

import asyncio
import sys
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

    from cuprum._capture_types import CaptureFinal, CaptureSink
    from cuprum.capture_policy import CaptureStreamName

_TEXT = _DecodeSettings(encoding="utf-8", errors="strict", capture_bytes=False)
# The same decode, but the child's own bytes are the payload. Both modes are
# exercised by the same cases, so a mode that quietly decoded — or a text mode
# that quietly returned bytes — cannot pass by running only its own arm.
_BYTES = _DecodeSettings(encoding="utf-8", errors="strict", capture_bytes=True)

# The concrete codec a mark-less ``utf-16``/``utf-32`` stream reads as: the
# decoders fall back to the platform's byte order when there is no mark.
_NATIVE_UTF16 = "utf-16-le" if sys.byteorder == "little" else "utf-16-be"
_NATIVE_UTF32 = "utf-32-le" if sys.byteorder == "little" else "utf-32-be"

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


async def _feed(sink: CaptureSink, chunks: cabc.Iterable[bytes]) -> None:
    """Await one absorb per chunk, in the order the drain would read them."""
    for chunk in chunks:
        await sink.absorb(chunk)


def _absorb(sink: CaptureSink, chunks: cabc.Iterable[bytes]) -> None:
    """Feed each chunk to *sink*, in order, on a loop of its own.

    The drain awaits the sink on a running loop, so a test that called the
    coroutine without one would test nothing. Driving it here keeps the cases
    below synchronous, and each call builds a fresh loop — the accumulator is
    built outside one and has to work across that boundary, which is how a
    config reused by a second run reaches it.
    """
    asyncio.run(_feed(sink, chunks))


def _finalize(sink: CaptureSink) -> CaptureFinal:
    """Read *sink*'s result, as a drain that reached the stream's end does."""
    return asyncio.run(sink.finalize())


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
    final = _finalize(sink)

    assert final.payload == expected, (
        f"a full capture must return the decoded stream; got {final.payload!r}"
    )
    assert final.capture.policy is CapturePolicyKind.FULL, (
        f"the record must name the full policy; got {final.capture.policy!r}"
    )
    assert final.capture.total_bytes == len(payload), (
        f"the record must count every byte; got {final.capture.total_bytes}"
    )
    assert final.capture.dropped_bytes == 0, "a full capture drops nothing"
    assert final.capture.storage == "memory", (
        f"a full capture stays on the heap; got {final.capture.storage!r}"
    )
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

    final = _finalize(sink)

    assert final.payload == payload, (
        f"byte mode must return the stream untouched; got {final.payload!r}"
    )


@pytest.mark.parametrize("payload", _CORPUS)
@pytest.mark.parametrize("max_bytes", [1, 2, 3, 4, 5, 8, 64, 4096])
def test_bounded_capture_retains_a_suffix_within_its_bound(
    payload: bytes,
    max_bytes: int,
) -> None:
    """The retained tail is a suffix of the stream, and never over the bound.

    Every bound from 1 to 4096 is exercised over a corpus whose multibyte
    entries are longer than several of those bounds, so the trim has real work
    to do rather than passing on payloads that fit whole. The bound is checked
    on the bytes the payload encodes back to, not on the record's own counts:
    reading the count back from the record would let a policy that retained
    more than it reported satisfy the bound with arithmetic it supplied
    itself.
    """
    sink = _sink(BoundedCapture(max_bytes=max_bytes))

    _absorb(sink, _chunks(payload, 1))
    final = _finalize(sink)
    record = final.capture
    assert isinstance(final.payload, str), "text mode must decode to text"
    retained = len(final.payload.encode())

    assert record.total_bytes == len(payload), (
        "the record must count the whole stream, not just the retained tail"
    )
    assert record.dropped_bytes + retained == len(payload), (
        "the lemma dropped + retained == total must hold; "
        f"got dropped={record.dropped_bytes}, retained={retained}, "
        f"total={record.total_bytes}"
    )
    assert retained <= max_bytes, (
        f"retention must stay within the bound; got {retained} bytes for "
        f"max_bytes={max_bytes}"
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

    within = _finalize(fits)
    beyond = _finalize(over)

    assert within.capture.truncated is False, (
        "a stream exactly as long as the bound drops nothing and is whole"
    )
    assert beyond.capture.truncated is (max_bytes < len(payload)), (
        "truncation must follow the drop count; "
        f"got {beyond.capture.dropped_bytes} dropped bytes "
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
    final = _finalize(sink)

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
    """Byte mode keeps the raw tail untrimmed, and the bound still holds.

    The front-drop in ``absorb`` alone keeps the buffer within ``max_bytes``;
    no code-point trim runs in byte mode.
    """
    payload = "\U0001f600\U0001f601\U0001f602".encode()
    sink = _sink(BoundedCapture(max_bytes=max_bytes), settings=_BYTES)

    _absorb(sink, [payload])
    final = _finalize(sink)

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
    final = _finalize(sink)

    assert final.payload == payload[1:].decode(), (
        f"the tail must be kept and the head dropped; got {final.payload!r}"
    )
    assert final.capture.dropped_bytes == 1, (
        f"exactly one byte is over the bound; got {final.capture.dropped_bytes}"
    )


@pytest.mark.parametrize("max_bytes", [1, 2, 3])
def test_a_repeated_finalize_reports_the_same_drop_count(max_bytes: int) -> None:
    """Reading the capture twice may not count the trim twice.

    A bound that lands inside a multibyte code point makes ``finalize`` drop
    the torn leading bytes, and a caller that reads the payload and the record
    from two calls would see the second call report a larger ``dropped_bytes``
    than the first — breaking ``dropped + retained == total`` for the record it
    actually holds. The payload is entirely four-byte code points, so every
    bound here forces the trim.
    """
    sink = _sink(BoundedCapture(max_bytes=max_bytes))

    _absorb(sink, ["\U0001f600\U0001f601".encode()])
    first = _finalize(sink)
    second = _finalize(sink)

    assert second.capture.dropped_bytes == first.capture.dropped_bytes, (
        "a repeated read must not accumulate the trim cost; "
        f"first reported {first.capture.dropped_bytes}, second "
        f"{second.capture.dropped_bytes}"
    )
    for final in (first, second):
        assert isinstance(final.payload, str), "text mode must decode to text"
        retained = len(final.payload.encode())
        assert final.capture.dropped_bytes + retained == final.capture.total_bytes, (
            "dropped + retained must equal the recorded total on every read; "
            f"got {final.capture.dropped_bytes} + {retained} != "
            f"{final.capture.total_bytes}"
        )


@pytest.mark.parametrize(
    ("encoding", "max_bytes", "expected"),
    [
        # The mark is 2 bytes for UTF-16 and 3 for UTF-8-SIG, so these bounds
        # leave the tail wholly mark-less. UTF-32's mark is 4 bytes, and a
        # 4-byte bound would retain it whole; 6 keeps a complete code unit
        # while still cutting the mark away.
        pytest.param("utf-16", 2, "中", id="utf-16"),
        pytest.param("utf-32", 6, "中", id="utf-32"),
        # ``中`` is three bytes in UTF-8, so a bound of 2 retains only part of
        # it and the leading trim drops what is left; a bound of 3 retains it
        # whole behind no continuation byte.
        pytest.param("utf-8-sig", 2, "", id="utf-8-sig-keeps-no-codepoint"),
        pytest.param("utf-8-sig", 3, "中", id="utf-8-sig-keeps-the-codepoint"),
    ],
)
def test_a_bound_that_discards_the_mark_still_decodes(
    encoding: str,
    max_bytes: int,
    expected: str,
) -> None:
    """A byte-order mark the bound dropped is resolved from the stream's head.

    ``utf-16``, ``utf-32``, and ``utf-8-sig`` select their real codec from a
    leading mark, and a decoder handed a mark-less tail raises rather than
    guessing an order. The stream is encoded *with* the family name, so the
    mark is really there, and the bound is set so the retained tail cannot
    contain it.

    The payload is compared exactly, not merely for being a suffix: every
    string ends with the empty string, so a suffix assertion would pass
    vacuously had the whole tail been trimmed away.
    """
    settings = _DecodeSettings(encoding, "strict", capture_bytes=False)
    sink = _sink(BoundedCapture(max_bytes=max_bytes), settings=settings)
    text = "ab中"

    _absorb(sink, [text.encode(encoding)])
    final = _finalize(sink)

    assert isinstance(final.payload, str), "text mode must decode to text"
    assert final.payload == expected, (
        f"a mark-less tail must still decode as the stream's own {encoding}; "
        f"got {final.payload!r}, expected {expected!r} from {text!r}"
    )


@pytest.mark.parametrize(
    ("family", "native", "max_bytes"),
    [
        # A code unit is 2 bytes wide, so a bound of one unit keeps exactly
        # the last character.
        pytest.param("utf-16", _NATIVE_UTF16, 2, id="utf-16"),
        # A code unit is 4 bytes wide here, so a bound of one unit keeps the
        # last character and nothing before it.
        pytest.param("utf-32", _NATIVE_UTF32, 4, id="utf-32"),
    ],
)
def test_a_mark_less_family_name_reads_the_tail_as_the_platform_does(
    family: str,
    native: str,
    max_bytes: int,
) -> None:
    """A family name with no mark decodes as its full-stream decoder would.

    ``utf-16`` and ``utf-32`` assume the platform's byte order when the stream
    carries no mark whose order they could read. A bound can cut the stream
    before any mark arrives, or the child may write no mark at all, so the
    tail must be trimmed and decoded under that same assumption rather than
    under the family name, which rejects a mark-less head outright.

    The payload is encoded with the concrete native codec, so the stream is
    genuinely mark-less; the family name is what the caller configured. A
    payload encoded with the family name would carry a mark, and then the
    head would resolve the codec and this rule would never be reached. The
    bound keeps one whole character, so a resolution that merely avoided the
    decoder's mark check could not pass by trimming everything away.
    """
    settings = _DecodeSettings(family, "strict", capture_bytes=False)
    sink = _sink(BoundedCapture(max_bytes=max_bytes), settings=settings)

    _absorb(sink, ["ab中".encode(native)])
    final = _finalize(sink)

    assert isinstance(final.payload, str), "text mode must decode to text"
    assert final.payload == "中", (
        f"a mark-less {family} tail must decode as native text; "
        f"got {final.payload!r} for {max_bytes} bytes"
    )


@pytest.mark.parametrize(
    ("family", "native", "max_bytes"),
    [
        # Two code units of four bytes each, so a bound of 5 keeps the second
        # character and one byte of the first.
        pytest.param("utf-16", _NATIVE_UTF16, 5, id="utf-16"),
        # One code unit is four bytes, so a bound of 5 keeps the second
        # character and the first byte of the first.
        pytest.param("utf-32", _NATIVE_UTF32, 5, id="utf-32"),
    ],
)
def test_a_mark_less_family_name_trims_the_leading_edge_to_its_units(
    family: str,
    native: str,
    max_bytes: int,
) -> None:
    """The platform rule applies to the leading edge, not only to the decode.

    A fixed-width encoding realigns by absolute stream offset, so the tail
    must be trimmed under the concrete codec's unit width. Under the family
    name the trim would leave the partial unit in place and the strict decode
    would raise; under UTF-8's width it would not move at all.
    """
    settings = _DecodeSettings(family, "strict", capture_bytes=False)
    sink = _sink(BoundedCapture(max_bytes=max_bytes), settings=settings)
    payload = "\U0001f600\U0001f601".encode(native)

    _absorb(sink, [payload])
    final = _finalize(sink)

    assert isinstance(final.payload, str), "text mode must decode to text"
    assert final.payload == "\U0001f601", (
        f"a mark-less {family} tail must keep the whole character the bound "
        f"covers and drop the partial one; got {final.payload!r} for "
        f"{max_bytes} bytes"
    )


@pytest.mark.parametrize(
    ("max_bytes", "expected"),
    [
        # ``中`` is three bytes in UTF-8. A bound of 2 retains only part of it,
        # which the leading trim drops, and a bound of 3 retains it whole.
        pytest.param(2, "", id="part-of-a-character"),
        pytest.param(3, "中", id="one-character"),
    ],
)
def test_a_mark_less_utf_8_sig_tail_trims_as_utf_8(
    max_bytes: int,
    expected: str,
) -> None:
    """``utf-8-sig`` accepts a stream with no mark, so a tail must too.

    The mark is optional when decoding, which makes ``utf-8-sig`` a valid way
    to say "UTF-8, BOM or not". A bound that dropped the head then leaves a
    plain UTF-8 tail, but the family name is not the codec the tail has to be
    *trimmed* under: it names a mark-stripping wrapper whose boundaries the
    leading trim cannot read. Resolving it to ``utf-8`` once the head is gone
    is what keeps a cut inside a code point from being decoded as a
    continuation byte.
    """
    settings = _DecodeSettings("utf-8-sig", "strict", capture_bytes=False)
    sink = _sink(BoundedCapture(max_bytes=max_bytes), settings=settings)

    _absorb(sink, ["ab中".encode()])
    final = _finalize(sink)

    assert isinstance(final.payload, str), "text mode must decode to text"
    assert final.payload == expected, (
        "a mark-less utf-8-sig tail must trim as UTF-8; "
        f"got {final.payload!r} for {max_bytes} bytes"
    )


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32", "utf-8-sig"])
def test_a_bound_the_stream_fits_reports_the_same_text_as_a_full_capture(
    encoding: str,
) -> None:
    """Resolving the codec early would leak the mark into the payload.

    A stream within the bound still carries its byte-order mark, and only the
    family codec strips it. Spelling the family as the concrete codec the mark
    names keeps the mark in the text, so a bounded capture would report a
    leading U+FEFF where a full capture of the same bytes reports none.
    """
    settings = _DecodeSettings(encoding, "strict", capture_bytes=False)
    payload = "abc".encode(encoding)
    full = _sink(FullCapture(), settings=settings)
    bounded = _sink(BoundedCapture(max_bytes=4096), settings=settings)

    _absorb(full, [payload])
    _absorb(bounded, [payload])
    final = _finalize(bounded)
    whole = _finalize(full)

    assert final.capture.dropped_bytes == 0, "the stream fits, so nothing was cut"
    assert final.payload == whole.payload, (
        f"a bound the stream fits must not change the decoded text for "
        f"{encoding}; got {final.payload!r}"
    )
    assert isinstance(final.payload, str), "text mode must decode to text"
    assert final.payload == "abc", (
        f"the mark must not survive as text; got {final.payload!r}"
    )


def test_build_accumulator_returns_nothing_for_an_uncaptured_stream() -> None:
    """No policy means no accumulator, which is how a discarded stream is said."""
    assert build_accumulator(None, stream="stdout", settings=_TEXT) is None, (
        "an absent policy must build no accumulator rather than an empty one"
    )


def test_build_accumulator_selects_the_file_backed_sink() -> None:
    """A file-backed policy reaches its own accumulator through the shared seam."""
    sink = build_accumulator(
        FileBackedCapture(),
        stream="stderr",
        settings=_TEXT,
    )

    assert sink is not None, "a file-backed policy must build an accumulator"
    final = _finalize(sink)

    assert final.capture.policy is CapturePolicyKind.FILE_BACKED, (
        "the file-backed policy must reach its own accumulator"
    )
    assert final.handle is None, "a capture that never spilled owns no file"


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
    assert isinstance(_sink(FullCapture()), _FullAccumulator), (
        "the full policy must build the full accumulator"
    )
    assert isinstance(_sink(BoundedCapture(max_bytes=8)), _BoundedAccumulator), (
        "the bounded policy must build the bounded accumulator"
    )


def _chunks(payload: bytes, size: int) -> list[bytes]:
    """Split *payload* into consecutive chunks of at most *size* bytes.

    Returns
    -------
    list[bytes]
        The chunks, in stream order, whose concatenation is *payload*.
    """
    return [payload[start : start + size] for start in range(0, len(payload), size)]
