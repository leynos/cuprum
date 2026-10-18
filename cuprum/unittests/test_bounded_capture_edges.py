"""Each boundary of a byte bound, stated as its own deterministic case.

``test_bounded_capture_properties.py`` holds the retention invariants over
generated input, and ``test_bounded_capture_seam.py`` proves the drain reaches
the accumulator. Neither states the boundaries a reader reasons about — a
stream exactly at the bound, one byte over it, a bound of one, an empty stream
— as cases with names. A property that samples them by luck and a seam test
that uses one bound together leave "what happens at the edge" answerable only
by inference.

The byte-versus-character cases are here for the same reason as the arithmetic
ones, and they carry the module's most important distinction: text mode trims
to a code-point boundary, and byte mode does not trim at all. The bound applies
to both; only the trim is text-only.
"""

from __future__ import annotations

import io
import typing as typ

import pytest

from cuprum._streams import _StreamConfig
from cuprum.capture_policy import BoundedCapture
from cuprum.echo_events import EchoStream
from cuprum.unittests._bounded_capture_support import _Retained, _sink
from cuprum.unittests._capture_sink_support import _drain_sink, _finalize

if typ.TYPE_CHECKING:
    from cuprum._capture_types import CaptureSink
    from cuprum.capture_policy import CapturePolicy


@pytest.mark.parametrize("max_bytes", [1, 7, 8, 9, 16, 4096])
def test_the_bound_is_a_byte_count_not_a_character_count(max_bytes: int) -> None:
    """A budget of *n* bytes retains at most *n* bytes of multi-byte text.

    Stated separately from the suffix property because it is the decision the
    module docstring commits to, and because a reader of the retention tests
    alone could take the bound for a character count: the same budget retains
    between a quarter and a whole as many characters, depending on the script.
    """
    payload = "中" * 64
    raw = payload.encode("utf-8")
    sink = _sink(BoundedCapture(max_bytes=max_bytes))

    final = _drain_sink(sink, [raw])
    retained = _Retained(payload=final.payload, record=final.capture)

    assert len(retained.retained_bytes()) <= max_bytes, (
        f"a byte bound must not be exceeded; got "
        f"{len(retained.retained_bytes())} for max_bytes={max_bytes}"
    )
    assert raw.endswith(retained.retained_bytes()), (
        "the retained bytes must be the stream's own tail; "
        f"got {retained.retained_bytes()!r} from {raw!r}"
    )
    assert final.capture.truncated is (len(raw) > max_bytes), (
        "a stream longer than its budget must report a truncation, and a "
        "stream within it must not"
    )


def test_a_zero_length_stream_is_a_complete_empty_capture() -> None:
    """The empty stream bounds trivially, and is not a truncation.

    The boundary that distinguishes "nothing was written" from "everything was
    dropped": with no bytes at all there is nothing to drop, so ``truncated``
    is ``False`` even under a bounded policy.
    """
    sink = _sink(BoundedCapture(max_bytes=1))

    final = _finalize(sink)

    # Absent and empty are different answers here: ``None`` is how a spilled
    # capture says the payload is the file, so an idle bounded capture has to
    # report empty text rather than borrow that signal for having nothing.
    assert final.payload is not None, "an idle bounded capture still reports text"
    assert not final.payload, (
        "an idle stream retains the empty string, not whatever was buffered"
    )
    assert final.capture.total_bytes == 0, (
        f"an idle stream wrote no bytes; got {final.capture.total_bytes}"
    )
    assert final.capture.dropped_bytes == 0, (
        f"an idle stream drops nothing; got {final.capture.dropped_bytes}"
    )
    assert final.capture.truncated is False, (
        "an idle stream is a complete empty capture, not a truncation"
    )


def test_a_bound_of_one_retains_nothing_from_a_multibyte_stream() -> None:
    """The smallest legal bound retains what it can — here, nothing.

    ``max_bytes=1`` is where the trim has no freedom: the final byte of a
    three-byte character is a continuation byte, so a strictly correct
    accumulator retains *nothing* rather than inventing a character. Stating
    that explicitly is what distinguishes "the bound held" from "the trim
    quietly grew the budget to fit a code point".
    """
    payload = "中".encode()
    sink = _sink(BoundedCapture(max_bytes=1))

    final = _drain_sink(sink, [payload])

    # A bound that retains nothing reports empty text, not an absent payload:
    # the accumulator decoded the stream and found no usable suffix, which is
    # a different outcome from a spilled capture's "read the file instead".
    assert final.payload is not None, "a bounded capture still reports text"
    assert not final.payload, (
        "no suffix of one byte decodes on its own, so nothing may be retained"
    )
    assert final.capture.total_bytes == len(payload), (
        f"the record must count the whole stream; got {final.capture.total_bytes}"
    )
    assert final.capture.dropped_bytes == len(payload), (
        "the bytes the trim cost must be counted as dropped"
    )
    assert final.capture.truncated is True, "a trimmed capture must report truncation"


def test_a_bound_exactly_the_stream_length_drops_nothing() -> None:
    """The bound is inclusive, so a stream its own length is not truncated."""
    payload = "中a".encode()
    sink = _sink(BoundedCapture(max_bytes=len(payload)))

    final = _drain_sink(sink, [payload])

    assert final.capture.dropped_bytes == 0, (
        f"an inclusive bound drops nothing; got {final.capture.dropped_bytes}"
    )
    assert final.capture.truncated is False, (
        "a capture that dropped nothing must not report truncation"
    )
    assert final.payload == payload.decode(), (
        f"a stream at the bound must survive whole; got {final.payload!r}"
    )


def test_a_bound_one_byte_short_of_the_stream_drops_exactly_one_byte() -> None:
    """The sharpest over-budget case: one byte over, one byte dropped."""
    payload = b"abcdefghij"
    sink = _sink(BoundedCapture(max_bytes=len(payload) - 1))

    final = _drain_sink(sink, [payload])

    assert final.capture.dropped_bytes == 1, (
        f"exactly one byte is over the bound; got {final.capture.dropped_bytes}"
    )
    assert final.payload == payload[1:].decode(), (
        f"the retained payload must be the stream minus its first byte; got "
        f"{final.payload!r}"
    )
    assert final.capture.truncated is True, (
        "a capture that dropped a byte must report truncation"
    )


def test_a_bound_larger_than_the_stream_retains_everything() -> None:
    """Over-provisioning the bound is a memory decision, not a capture one."""
    payload = b"short"
    sink = _sink(BoundedCapture(max_bytes=4096))

    final = _drain_sink(sink, [payload])

    assert final.payload == payload.decode(), (
        f"a stream under the bound must survive whole; got {final.payload!r}"
    )
    assert final.capture.total_bytes == len(payload), (
        f"the record must count every byte; got {final.capture.total_bytes}"
    )
    assert final.capture.dropped_bytes == 0, (
        f"a stream under the bound drops nothing; got {final.capture.dropped_bytes}"
    )
    assert final.capture.truncated is False, (
        "a capture that dropped nothing must not report truncation"
    )


def test_the_accumulator_counts_bytes_across_arbitrary_chunk_partitions() -> None:
    """A stream split across reads is accounted for as one stream.

    The caller's read size is not the child's write size, so a payload arrives
    in an arbitrary partition. The bound must apply to the concatenation rather
    than per chunk: a per-chunk bound would retain up to ``max_bytes`` for
    every read, which is the unbounded behaviour the policy exists to prevent.
    """
    payload = bytes(range(0x61, 0x7B)) * 4
    sink = _sink(BoundedCapture(max_bytes=8))

    final = _drain_sink(
        sink,
        (payload[start : start + 3] for start in range(0, len(payload), 3)),
    )

    assert final.capture.total_bytes == len(payload), (
        f"the count must span the whole stream; got {final.capture.total_bytes}"
    )
    assert final.capture.dropped_bytes == len(payload) - 8, (
        "the drop count must be measured against the concatenated stream, not "
        f"per chunk; got {final.capture.dropped_bytes}"
    )
    assert final.payload == payload[-8:].decode(), (
        "the retained tail must be the stream's last eight bytes, not the "
        "last eight bytes of each read"
    )


def _byte_sink(policy: CapturePolicy) -> CaptureSink:
    """Return the accumulator a byte-exact run with *policy* retains into."""
    config = _StreamConfig(
        capture=policy,
        echo_output=False,
        # The accumulator is all this helper needs, and a byte-exact run's
        # sink is never written to by it; a text sink keeps the config's
        # declared type and avoids a bytes stream the echo would never use.
        sink=io.StringIO(),
        encoding="utf-8",
        errors="strict",
        stream=EchoStream.STDOUT,
        capture_bytes=True,
    )
    sink = config.capture_sink
    assert sink is not None, "a named policy must build an accumulator"
    return sink


def test_a_byte_mode_bound_hands_back_the_child_bytes_untouched() -> None:
    r"""A byte-exact capture is never trimmed to a code-point boundary.

    The trim exists to stop a text decode raising or growing a replacement
    character. A byte-exact run does not decode, so a binary tail that happens
    to end in a continuation-shaped byte — ``b"\\xc3"`` is the leading byte of
    a UTF-8 two-byte character and also an ordinary byte in a binary payload —
    must survive untouched. The bound still applies, and only the bound: the
    payload here is one byte over it, so the case would also catch a byte mode
    that quietly skipped the retention limit.
    """
    payload = b"\x00\x01\x02\x03\xc3"
    sink = _byte_sink(BoundedCapture(max_bytes=4))

    final = _drain_sink(sink, [payload])

    assert final.payload == payload[-4:], (
        "a byte-exact capture must return the child's own bytes, cut only by "
        f"the bound; got {final.payload!r}"
    )
    assert final.capture.dropped_bytes == 1, (
        "a byte-exact capture counts only the bytes the bound discarded; got "
        f"{final.capture.dropped_bytes}"
    )
    assert final.capture.truncated is True, (
        "a byte-exact capture that met its bound by dropping a byte is truncated"
    )


def test_a_byte_mode_bound_keeps_a_continuation_shaped_leading_byte() -> None:
    """A byte-exact tail starting with ``0x80``-``0xBF`` is not a torn code point.

    The stream begins with a real three-byte character and is cut inside it, so
    a byte mode that applied the text trim would walk the cut forward over the
    ``0x80``-``0xBF`` bytes and drop bytes the bound already accounted for.
    """
    payload = "中".encode() + b"\x80\x81\x82"
    sink = _byte_sink(BoundedCapture(max_bytes=3))

    final = _drain_sink(sink, [payload])

    assert final.payload == b"\x80\x81\x82", (
        "a binary prefix must not be mistaken for continuation bytes, so the "
        f"bound alone decides the tail; got {final.payload!r}"
    )
    assert final.capture.dropped_bytes == len(payload) - 3, (
        "the 0x80-0xBF leading bytes must not be counted as dropped by a trim "
        f"that byte mode never makes; got {final.capture.dropped_bytes}"
    )


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_a_fixed_width_encoding_realigns_the_retained_tail(encoding: str) -> None:
    """A UTF-16 bound cuts by bytes but must not misalign the code units.

    A UTF-16 low byte can fall in the ``0x80``-``0xBF`` range a UTF-8
    continuation byte occupies, so a per-byte skip would drop an ordinary code
    unit's first byte and misalign everything after it. The tail is realigned
    by stream offset instead, so the decoded suffix is a suffix of the text.
    """
    text = "a中b文c"
    payload = text.encode(encoding)
    config = _StreamConfig(
        capture=BoundedCapture(max_bytes=len(payload) - 3),
        echo_output=False,
        sink=io.StringIO(),
        encoding=encoding,
        errors="strict",
        stream=EchoStream.STDOUT,
    )
    sink = config.capture_sink
    assert sink is not None, "a named policy must build an accumulator"

    final = _drain_sink(sink, (bytes([byte]) for byte in payload))

    assert isinstance(final.payload, str), "text mode must decode to text"
    assert text.endswith(final.payload), (
        "a realigned tail must be a suffix of the text the child wrote; "
        f"got {final.payload!r} from {text!r}"
    )
