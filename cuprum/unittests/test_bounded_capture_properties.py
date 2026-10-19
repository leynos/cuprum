"""The bounded capture's retention invariants, over generated input.

The generator is the part that matters. A page of handwritten examples is
where a bound bug survives: every case a person writes is a case they already
had in mind. The properties here therefore run over generated text and publish
each example's retention class through ``hypothesis.event``, so a run's
statistics show whether a stream longer than its bound, a stream shorter, an
exactly-fitting stream, and a tail that begins inside a code point were
actually exercised. Statistics alone do not fail a run that misses a class, so
the corpus tests below pin that each class is reachable through the same
accumulator seam.

Text generation alone cannot witness the byte-exact contract: a text property
reaches the accumulator through UTF-8, so it can only ever see byte strings a
decoder would accept. The byte-mode properties below therefore generate
arbitrary binary payloads and split them into arbitrary chunk sequences,
because the accumulator's contract is over the bytes the child wrote, not over
the text they happen to encode. Chunking matters independently: an accumulator
that reset its counts per read, or that retained a suffix per chunk, would pass
a single-chunk property.

The deterministic boundary cases are in ``test_bounded_capture_edges.py`` and
the drain-seam cases in ``test_bounded_capture_seam.py``; this module is only
about what holds for *any* stream and bound.
"""

from __future__ import annotations

import itertools

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from cuprum.capture_policy import BoundedCapture
from cuprum.unittests._bounded_capture_support import _byte_sink, _Retained, _sink
from cuprum.unittests._capture_sink_support import _drain_sink

_PROPERTY_MAX_EXAMPLES = 150
# Long enough that a bound of 1 spends most of the stream dropping, short
# enough that a hundred and fifty examples stay cheap. It bounds the generated
# *characters*, which is what the text strategy below measures; the binary
# strategy bounds its payloads in bytes instead.
_PAYLOAD_MAX_CHARS = 48
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
_PAYLOADS = st.text(alphabet=_TEXT_ALPHABET, max_size=_PAYLOAD_MAX_CHARS)
# Bounded by the largest possible encoded size, so a bound can fall beyond the
# stream as well as inside it.
_BOUNDS = st.integers(min_value=1, max_value=4 * _PAYLOAD_MAX_CHARS + 8)
# Arbitrary bytes, deliberately hostile to a decoder: a stream can begin inside
# a multi-byte character, contain lone continuation bytes, or hold a NUL. A
# byte-exact capture must survive all of them because it never decodes.
_BINARY_PAYLOADS = st.binary(max_size=_PAYLOAD_MAX_CHARS)
# Cut positions inside the payload, so a stream arrives as an arbitrary number
# of chunks rather than as one read. Sorted and de-duplicated where they are
# used, because Hypothesis does not promise an ordered list.
_BINARY_CUTS = st.lists(
    st.integers(min_value=0, max_value=_PAYLOAD_MAX_CHARS),
    max_size=8,
)


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


def _retain(
    payload: bytes,
    max_bytes: int,
    stats: _Stats | None = None,
) -> _Retained:
    """Retain *payload* under a bound, through the accumulator seam.

    Returns
    -------
    _Retained
        The finalized payload and record, both taken from the accumulator.
    """
    sink = _sink(BoundedCapture(max_bytes=max_bytes))
    final = _drain_sink(sink, [payload])
    record = final.capture
    if stats is not None:
        stats.over_budget += len(payload) > max_bytes
        stats.under_budget += len(payload) < max_bytes
        stats.exactly_budget += len(payload) == max_bytes
        stats.multibyte += any(byte >= 0x80 for byte in payload)
        stats.trimmed += record.dropped_bytes > max(0, len(payload) - max_bytes)
    return _Retained(payload=final.payload, record=record)


def _announce_retention(payload: bytes, retained: _Retained, max_bytes: int) -> None:
    """Publish this example's classes to the property run's statistics.

    Only the Hypothesis properties call this: ``event`` is invalid outside a
    generated example, and the deterministic corpus tests share the same
    retention seam without being generated runs.
    """
    event("over-budget" if len(payload) > max_bytes else "within-budget")
    event("multibyte" if any(byte >= 0x80 for byte in payload) else "ascii")
    # A trim that cost more than the overflow is the class the suffix property
    # exists for: the bound was met by cutting into a code point.
    if retained.record.dropped_bytes > max(0, len(payload) - max_bytes):
        event("trimmed")


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
    retained = _retain(raw, max_bytes)
    _announce_retention(raw, retained, max_bytes)
    record = retained.record

    assert record.total_bytes == len(raw), (
        "the record must count the whole stream, not just the tail"
    )
    kept = retained.retained_bytes()
    assert len(kept) <= max_bytes, (
        f"retention must fit the bound; got {len(kept)} bytes for max_bytes={max_bytes}"
    )
    assert raw.endswith(kept), (
        f"the retained bytes must be the stream's own tail; got {kept!r} from {raw!r}"
    )
    assert record.dropped_bytes + len(kept) == record.total_bytes, (
        "the lemma dropped + retained == total must hold; "
        f"got {record.dropped_bytes} dropped of {record.total_bytes}"
    )


def _partition(payload: bytes, cuts: list[int]) -> list[bytes]:
    """Split *payload* at *cuts*, as an arbitrary sequence of reads.

    Returns
    -------
    list[bytes]
        The payload's chunks, in stream order. An empty payload yields no
        chunks; a cut past the end is ignored, so every chunk is non-empty
        and the concatenation is always *payload*.
    """
    boundaries = sorted({0, len(payload), *(c for c in cuts if c < len(payload))})
    # Adjacent pairs, not a sliding window zipped against itself: the two
    # iterables would differ in length by one and ``strict=True`` would refuse
    # every case.
    return [
        payload[start:end]
        for start, end in itertools.pairwise(boundaries)
        if end > start
    ]


@given(payload=_BINARY_PAYLOADS, max_bytes=_BOUNDS, cuts=_BINARY_CUTS)
@settings(
    max_examples=_PROPERTY_MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_a_byte_exact_capture_retains_the_exact_byte_tail(
    payload: bytes,
    max_bytes: int,
    cuts: list[int],
) -> None:
    """Byte mode returns exactly the stream's last ``max_bytes`` bytes.

    This is O1's byte-exact half, stated over generated binary rather than over
    encoded text, because the two properties differ in what they can see. A
    text property can never produce a payload whose UTF-8 is invalid, so it can
    never witness that byte mode leaves arbitrary bytes untouched; and it
    reaches the accumulator with one chunk, so it cannot witness that the
    counts and the retained tail are measured against the concatenated stream.
    The properties below are asserted against the source bytes rather than the
    record's own arithmetic, for the same reason as the text property: a
    count that agreed with a wrongly retained slice would prove nothing.

    The exact tail — not merely a suffix — is what the bound promises. Any
    stream shorter than the bound is returned whole, so the same assertion
    covers the no-drop case, and ``dropped + kept == total`` covers the count.
    """
    chunks = _partition(payload, cuts)
    sink = _byte_sink(BoundedCapture(max_bytes=max_bytes))
    final = _drain_sink(sink, chunks)
    record = final.capture

    event("over-budget" if len(payload) > max_bytes else "within-budget")
    event("binary" if any(byte >= 0x80 for byte in payload) else "ascii")
    event("chunked" if len(chunks) > 1 else "single-read")

    assert isinstance(final.payload, bytes), (
        f"byte mode must return bytes, not {type(final.payload).__name__}"
    )
    expected = payload[-max_bytes:]
    assert final.payload == expected, (
        "byte mode must return the stream's exact byte tail; "
        f"got {final.payload!r}, expected {expected!r} for max_bytes={max_bytes}"
    )
    assert record.total_bytes == len(payload), (
        "the record must count the whole stream, not just the tail; "
        f"got {record.total_bytes} for {len(payload)} bytes"
    )
    assert record.dropped_bytes == max(0, len(payload) - max_bytes), (
        "byte mode drops exactly the bytes beyond the bound, never more; "
        f"got {record.dropped_bytes} for max_bytes={max_bytes}"
    )
    assert record.dropped_bytes + len(final.payload) == record.total_bytes, (
        "the lemma dropped + retained == total must hold; "
        f"got {record.dropped_bytes} dropped of {record.total_bytes}"
    )
    assert record.truncated is (len(payload) > max_bytes), (
        "truncation is exactly the over-budget case; "
        f"got {record.truncated} for {len(payload)} bytes at max_bytes={max_bytes}"
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
    raw = payload.encode("utf-8")
    sink = _sink(BoundedCapture(max_bytes=max_bytes))

    final = _drain_sink(sink, [raw])
    _announce_retention(
        raw,
        _Retained(payload=final.payload, record=final.capture),
        max_bytes,
    )

    assert isinstance(final.payload, str), "text mode must decode to text"
    assert payload.endswith(final.payload), (
        "a whole-code-point tail must be a suffix of the text the child wrote; "
        f"got {final.payload!r} from {payload!r}"
    )


def test_the_corpus_reaches_every_class_the_properties_need() -> None:
    """The accumulator can reach every class the properties depend on.

    This corpus is a fixed witness of the accumulator's reachability, not a
    statement about the generator: it names its own payloads and bounds, so an
    edit to ``_TEXT_ALPHABET`` or ``_BOUNDS`` cannot affect it. Whether the
    *generated* examples cover these classes is what the properties publish
    through ``hypothesis.event``; a missing class shows up in a run's
    statistics rather than as a failure here.
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
