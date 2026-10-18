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

The deterministic boundary cases are in ``test_bounded_capture_edges.py`` and
the drain-seam cases in ``test_bounded_capture_seam.py``; this module is only
about what holds for *any* stream and bound.
"""

from __future__ import annotations

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from cuprum.capture_policy import BoundedCapture
from cuprum.unittests._bounded_capture_support import _Retained, _sink
from cuprum.unittests._capture_sink_support import _drain_sink

_PROPERTY_MAX_EXAMPLES = 150
# Long enough that a bound of 1 spends most of the stream dropping, short
# enough that a hundred and fifty examples stay cheap. It bounds the generated
# *characters*, which is what the strategy below measures.
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
