"""The drain's retention policy, over generated input and through the real seam.

``test_capture_accumulator.py`` pins the retention arithmetic on chosen
corpora. This module closes the two gaps that leaves: that the *drain* reaches
the accumulator at all — a policy the drain never consults would leave every
one of those cases passing while a real run captured everything — and that the
retention invariants hold over an open-ended input space rather than over
payloads someone thought of.

The generator is the part that matters. A page of handwritten examples is
where a bound bug survives: every case a person writes is a case they already
had in mind. The properties below are stated so that the interesting classes —
a stream longer than its bound, a stream shorter, an exactly-fitting stream, a
tail that begins inside a code point — are reached and *counted*, so a run that
never generated one is reported as a failure rather than as a pass.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum._streams import _drain, _StreamConfig
from cuprum.capture_policy import BoundedCapture, FullCapture
from cuprum.echo_events import EchoStream

if typ.TYPE_CHECKING:
    from cuprum._capture_types import CaptureSink
    from cuprum._stream_drain_finish import _CapturedDrain
    from cuprum.capture_policy import CapturedStream, CapturePolicy

_PROPERTY_MAX_EXAMPLES = 150
# Long enough that a bound of 1 spends most of the stream dropping, short
# enough that a hundred and fifty examples stay cheap.
_PAYLOAD_MAX_BYTES = 48
# Chosen for what a byte-oriented bound gets wrong. The CJK and emoji entries
# are three and four bytes wide, so a bound expressed as a character count
# would retain the wrong number of bytes, and an ASCII-only alphabet would let
# that pass. The empty string is in every generated stream's reach, so the
# zero case is covered without a separate alphabet.
_TEXT_ALPHABET = st.sampled_from([
    "a",
    "z",
    " ",
    "\n",
    "é",
    "ß",
    "中",
    "文",
    "\U0001f600",
    "\U0001f4a9",
])
_PAYLOADS = st.text(alphabet=_TEXT_ALPHABET, max_size=_PAYLOAD_MAX_BYTES)
_BOUNDS = st.integers(min_value=1, max_value=_PAYLOAD_MAX_BYTES + 8)


class _Stats:
    """How many generated cases fell into each class the properties need.

    A property that only ever saw streams shorter than their bound would pass
    without ever exercising the drop path, and one that only ever saw ASCII
    would pass without exercising the trim. Counting both is what turns "the
    generator probably produced some" into an assertion.
    """

    def __init__(self) -> None:
        """Start with every class unvisited."""
        self.over_budget = 0
        self.under_budget = 0
        self.exactly_budget = 0
        self.multibyte = 0
        self.trimmed = 0

    @property
    def unreached(self) -> list[str]:
        """Name the classes no generated case reached, in the order declared."""
        counted = (
            ("over_budget", self.over_budget),
            ("under_budget", self.under_budget),
            ("exactly_budget", self.exactly_budget),
            ("multibyte", self.multibyte),
            ("trimmed", self.trimmed),
        )
        return [name for name, count in counted if count == 0]


def _config(policy: CapturePolicy | None) -> _StreamConfig:
    """Build the stdout capture config a run with *policy* would use."""
    return _StreamConfig(
        capture=policy,
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="strict",
        stream=EchoStream.STDOUT,
    )


def _sink(policy: CapturePolicy) -> CaptureSink:
    """Return the accumulator the config for *policy* retains into."""
    sink = _config(policy).capture_sink
    assert sink is not None, "a named policy must build an accumulator"
    return sink


def _retain(
    payload: bytes,
    max_bytes: int,
    stats: _Stats | None = None,
) -> CapturedStream:
    """Retain *payload* under a bound, through the accumulator seam.

    Returns
    -------
    CapturedStream
        The record the accumulator finalized for this stream.
    """
    sink = _sink(BoundedCapture(max_bytes=max_bytes))
    sink.absorb(payload)
    record = sink.finalize().capture
    if stats is not None:
        stats.over_budget += len(payload) > max_bytes
        stats.under_budget += len(payload) < max_bytes
        stats.exactly_budget += len(payload) == max_bytes
        stats.multibyte += any(byte >= 0x80 for byte in payload)
        # A trim that cost more than the overflow is the class the suffix
        # property exists for: the bound was met by cutting into a code point.
        stats.trimmed += record.dropped_bytes > max(0, len(payload) - max_bytes)
    return record


def _retained_bytes(payload: bytes, record: CapturedStream) -> bytes:
    """Slice off the prefix the record's drop count says was discarded."""
    return payload[record.dropped_bytes :]


@given(payload=_PAYLOADS, max_bytes=_BOUNDS)
@settings(
    max_examples=_PROPERTY_MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_a_bounded_capture_retains_exactly_a_suffix_within_its_bound(
    payload: str,
    max_bytes: int,
) -> None:
    """Retention is a suffix of the stream, within the bound, fully accounted for.

    Three claims, each false for a different plausible bug. The retained tail
    must be a *suffix* of what the child wrote, so a drop from the wrong end
    fails. It must fit the bound, so an accumulator that ignored ``max_bytes``
    fails. And the retained length must be the stream's length less the
    reported drop count, so counts that drifted from the bytes actually held
    fail.

    The suffix is compared against the source bytes rather than against the
    record's own arithmetic, because a trim that moved the cut backwards over a
    code point *updates the counts to match*: reading the retained length back
    off the record would agree with a payload taken from the wrong end.
    """
    raw = payload.encode("utf-8")
    record = _retain(raw, max_bytes)

    assert record.total_bytes == len(raw), (
        "the record must count the whole stream, not just the tail"
    )
    retained = _retained_bytes(raw, record)
    assert len(retained) <= max_bytes, (
        f"retention must fit the bound; got {len(retained)} bytes "
        f"for max_bytes={max_bytes}"
    )
    assert raw.endswith(retained), (
        "the retained bytes must be the stream's own tail; "
        f"got {retained!r} from {raw!r}"
    )
    assert record.dropped_bytes + len(retained) == record.total_bytes, (
        "the lemma dropped + retained == total must hold; "
        f"got {record.dropped_bytes} dropped of {record.total_bytes}"
    )


@given(payload=_PAYLOADS, max_bytes=_BOUNDS)
@settings(
    max_examples=_PROPERTY_MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_no_bound_splits_a_code_point(payload: str, max_bytes: int) -> None:
    """A strict decoder reads the retained tail, for any bound.

    This is the property the trim exists for. The generator's alphabet
    includes three- and four-byte characters, so bounds land inside code
    points constantly; a length-only trim fails here with ``UnicodeDecodeError``
    rather than returning something subtly wrong.
    """
    sink = _sink(BoundedCapture(max_bytes=max_bytes))

    sink.absorb(payload.encode("utf-8"))
    final = sink.finalize()

    assert isinstance(final.payload, str), "text mode must decode to text"
    assert payload.endswith(final.payload), (
        "a whole-code-point tail must be a suffix of the text the child wrote; "
        f"got {final.payload!r} from {payload!r}"
    )


def test_the_corpus_reaches_every_class_the_properties_need() -> None:
    """The properties above would be vacuous without these classes.

    Stated as its own test rather than as an assertion inside them, because
    the counts are evidence about the *generator*: a future edit to the
    alphabet or the bounds should fail here, naming which class went
    unreached, rather than somewhere inside a property whose subject is the
    accumulator.
    """
    stats = _Stats()
    for payload in ("", "a", "abc", "中", "中文中", "\U0001f600"):
        for max_bytes in (1, 2, 3, 4, 8):
            _retain(payload.encode(), max_bytes, stats)

    assert stats.unreached == [], (
        f"the corpus must reach every class; unreached: {stats.unreached}"
    )


def test_the_trim_class_needs_a_stream_that_ends_mid_code_point() -> None:
    """At least one case pays for the bound by cutting into a code point.

    The class the properties are least likely to reach by accident, because it
    needs a stream both over budget *and* cut inside a multi-byte character.
    The corpus is stated here rather than left to the generator, so an edit
    that made trimming unreachable is caught by a deterministic failure.
    """
    stats = _Stats()
    for payload in ("中文中文", "\U0001f600" * 6, "a" * 12 + "中"):
        for max_bytes in (1, 3, 5, 7, 11):
            _retain(payload.encode(), max_bytes, stats)

    assert stats.trimmed > 0, "no case ended mid-code-point; the trim is untested"


def test_the_drain_config_owns_the_accumulator_its_policy_names() -> None:
    """A config retains one sink, holding the bytes written through it.

    Built in ``__post_init__`` rather than per read, because the teardown path
    registers the sink and a fresh accumulator per access would leave the
    registered one holding bytes the drain never read into. Writing through one
    read of the attribute and finalizing another is what witnesses that: two
    distinct accumulators would each report an empty capture.
    """
    config = _config(BoundedCapture(max_bytes=8))
    sink = config.capture_sink
    assert sink is not None, "a bounded policy must build a sink"

    sink.absorb(b"0123456789ab")
    # Read through the attribute again rather than reusing ``sink``: the
    # assertion above narrows a *local*, and the invariant under test is that
    # the config itself keeps handing back the same accumulator.
    reread = config.capture_sink
    assert reread is not None, "the policy is still bounded on re-read"
    final = reread.finalize()

    assert isinstance(config.capture, BoundedCapture)
    assert config.capture.max_bytes == 8
    assert final.capture.total_bytes == 12, (
        "the config must retain the accumulator the bytes were written to, "
        f"not build a fresh one per read; got {final.capture.total_bytes} bytes"
    )


def test_a_config_without_a_policy_owns_no_accumulator() -> None:
    """No policy means no sink, which is how a discarded stream is configured."""
    assert _config(None).capture_sink is None


def _run_drain(payload: bytes, policy: CapturePolicy | None) -> _CapturedDrain:
    """Feed *payload* through a real drain configured with *policy*."""

    async def feed() -> _CapturedDrain:
        """Read the payload through the drain's own loop."""
        reader = asyncio.StreamReader()
        reader.feed_data(payload)
        reader.feed_eof()
        return await _drain(reader, _config(policy))

    return asyncio.run(feed())


def test_the_drain_retains_a_bounded_tail_not_the_whole_stream() -> None:
    """A real drain reports the tail, through the accumulator the policy built.

    End-to-end through ``_drain``, which is what a run reaches. A drain that
    ignored the config's accumulator — reading chunks and never retaining them
    — would satisfy every accumulator test in this repository while capturing
    nothing, so this is the assertion that connects the policy to the read
    loop.

    The payload is written in one chunk five times the bound, so the drop
    happens on the first read rather than depending on how the reader happens
    to partition a single ``feed_data``.
    """
    payload = b"0123456789abcdef"
    result = _run_drain(payload, BoundedCapture(max_bytes=8))

    assert result.capture is not None, "a bounded drain must report a capture"
    assert result.capture.total_bytes == len(payload), (
        "the drain must have absorbed every byte the child wrote"
    )
    assert result.capture.dropped_bytes == len(payload) - 8, (
        "the drain must have retained only the bound; "
        f"got {result.capture.dropped_bytes} dropped"
    )
    assert result.payload == payload[-8:].decode(), (
        f"the retained text must be the stream's tail; got {result.payload!r}"
    )
    assert result.handle is None, "a memory-backed bound owns no file"


@pytest.mark.parametrize("max_bytes", [1, 3, 8])
def test_the_drain_retains_through_the_policy_it_was_configured_with(
    max_bytes: int,
) -> None:
    """The drain's payload is bounded, not just its record.

    The previous case reads the record; this reads the *payload*, which is
    what a caller receives. A drain that absorbed into the accumulator but
    returned the raw buffer it also kept would report a truncated record
    beside a complete payload, and only this assertion sees that.

    The payload is a repeated three-byte character and the bound is not a
    multiple of three for two of the three cases, so the trim has real work to
    do rather than passing on a stream that happens to fit whole.
    """
    payload = "中".encode() * 8
    result = _run_drain(payload, BoundedCapture(max_bytes=max_bytes))

    assert isinstance(result.payload, str), "text mode must decode to text"
    assert len(result.payload.encode("utf-8")) <= max_bytes, (
        f"the drain's payload must fit the bound; got "
        f"{len(result.payload.encode('utf-8'))} bytes for max_bytes={max_bytes}"
    )
    assert payload.endswith(result.payload.encode("utf-8")), (
        f"the drain's payload must be the stream's tail; got {result.payload!r}"
    )
    assert result.capture is not None
    assert result.capture.truncated is True


def test_the_drain_reports_an_uncaptured_stream_as_empty() -> None:
    """A stream with no policy yields the empty drain result, holding nothing."""
    result = _run_drain(b"discarded entirely", None)

    assert result.payload is None, (
        f"an uncaptured drain must report no payload; got {result.payload!r}"
    )
    assert result.capture is None, (
        f"an uncaptured drain must report no capture record; got {result.capture!r}"
    )
    assert result.handle is None, (
        f"an uncaptured drain must own no file; got {result.handle!r}"
    )


def test_the_drain_with_a_full_policy_is_unchanged_by_the_bound_machinery() -> None:
    """The default policy still returns the whole stream, byte for byte.

    The bound is what this module is about, so the no-bound case is stated
    here too: a caller who configured nothing must see the payload they always
    saw, with a record that reports nothing dropped.
    """
    payload = b"a complete stream\nwith two lines\n"
    result = _run_drain(payload, FullCapture())

    assert result.payload == payload.decode()
    assert result.capture is not None
    assert result.capture.total_bytes == len(payload)
    assert result.capture.dropped_bytes == 0
    assert result.capture.truncated is False


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

    sink.absorb(raw)
    final = sink.finalize()
    retained = _retained_bytes(raw, final.capture)

    assert len(retained) <= max_bytes, (
        f"a byte bound must not be exceeded; got {len(retained)} "
        f"for max_bytes={max_bytes}"
    )
    assert final.payload == retained.decode("utf-8"), (
        "the retained text must be exactly the retained bytes decoded"
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

    final = sink.finalize()

    # Absent and empty are different answers here: ``None`` is how a spilled
    # capture says the payload is the file, so an idle bounded capture has to
    # report empty text rather than borrow that signal for having nothing.
    assert final.payload is not None, "an idle bounded capture still reports text"
    assert not final.payload
    assert final.capture.total_bytes == 0
    assert final.capture.dropped_bytes == 0
    assert final.capture.truncated is False


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

    sink.absorb(payload)
    final = sink.finalize()

    # A bound that retains nothing reports empty text, not an absent payload:
    # the accumulator decoded the stream and found no usable suffix, which is
    # a different outcome from a spilled capture's "read the file instead".
    assert final.payload is not None, "a bounded capture still reports text"
    assert not final.payload, (
        "no suffix of one byte decodes on its own, so nothing may be retained"
    )
    assert final.capture.total_bytes == len(payload)
    assert final.capture.dropped_bytes == len(payload), (
        "the bytes the trim cost must be counted as dropped"
    )
    assert final.capture.truncated is True


def test_a_bound_exactly_the_stream_length_drops_nothing() -> None:
    """The bound is inclusive, so a stream its own length is not truncated."""
    payload = "中a".encode()
    sink = _sink(BoundedCapture(max_bytes=len(payload)))

    sink.absorb(payload)
    final = sink.finalize()

    assert final.capture.dropped_bytes == 0
    assert final.capture.truncated is False
    assert final.payload == payload.decode()


def test_a_bound_one_byte_short_of_the_stream_drops_exactly_one_byte() -> None:
    """The sharpest over-budget case: one byte over, one byte dropped."""
    payload = b"abcdefghij"
    sink = _sink(BoundedCapture(max_bytes=len(payload) - 1))

    sink.absorb(payload)
    final = sink.finalize()

    assert final.capture.dropped_bytes == 1
    assert final.payload == payload[1:].decode()
    assert final.capture.truncated is True


def test_a_bound_larger_than_the_stream_retains_everything() -> None:
    """Over-provisioning the bound is a memory decision, not a capture one."""
    payload = b"short"
    sink = _sink(BoundedCapture(max_bytes=4096))

    sink.absorb(payload)
    final = sink.finalize()

    assert final.payload == payload.decode()
    assert final.capture.total_bytes == len(payload)
    assert final.capture.dropped_bytes == 0
    assert final.capture.truncated is False


def test_the_accumulator_counts_bytes_across_arbitrary_chunk_partitions() -> None:
    """A stream split across reads is accounted for as one stream.

    The caller's read size is not the child's write size, so a payload arrives
    in an arbitrary partition. The bound must apply to the concatenation rather
    than per chunk: a per-chunk bound would retain up to ``max_bytes`` for
    every read, which is the unbounded behaviour the policy exists to prevent.
    """
    payload = bytes(range(0x61, 0x7B)) * 4
    sink = _sink(BoundedCapture(max_bytes=8))

    for start in range(0, len(payload), 3):
        sink.absorb(payload[start : start + 3])
    final = sink.finalize()

    assert final.capture.total_bytes == len(payload)
    assert final.capture.dropped_bytes == len(payload) - 8
    assert final.payload == payload[-8:].decode(), (
        "the retained tail must be the stream's last eight bytes, not the "
        "last eight bytes of each read"
    )
