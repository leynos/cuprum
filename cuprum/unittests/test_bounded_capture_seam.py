"""The drain reaches the accumulator the policy configured.

``test_capture_accumulator.py`` pins the retention arithmetic on chosen
corpora, and ``test_bounded_capture_properties.py`` holds it over generated
input. Both drive the accumulator directly, so both would pass unchanged if the
drain never consulted the config's sink at all — a real run would capture
everything while every one of those cases stayed green. This module closes that
gap: it reads each payload through ``_drain`` itself, which is what a run
reaches, and asserts on what the drain returned rather than on what the
accumulator was told.

The config-ownership cases live here too. A sink built per access rather than
once in ``__post_init__`` would still pass every accumulator test, and would
leave the teardown path holding bytes the drain never wrote into.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum._streams import _drain
from cuprum.capture_policy import BoundedCapture, FullCapture
from cuprum.unittests._bounded_capture_support import _config
from cuprum.unittests._capture_sink_support import _absorb, _finalize

if typ.TYPE_CHECKING:
    from cuprum._stream_drain_finish import _CapturedDrain
    from cuprum.capture_policy import CapturePolicy


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

    _absorb(sink, [b"0123456789ab"])
    # Read through the attribute again rather than reusing ``sink``: the
    # assertion above narrows a *local*, and the invariant under test is that
    # the config itself keeps handing back the same accumulator.
    reread = config.capture_sink
    assert reread is not None, "the policy is still bounded on re-read"
    final = _finalize(reread)

    assert isinstance(config.capture, BoundedCapture), (
        f"the config must report the bounded policy it was given; got "
        f"{config.capture!r}"
    )
    assert config.capture.max_bytes == 8, (
        f"the config must keep the original bound; got {config.capture.max_bytes}"
    )
    assert final.capture.total_bytes == 12, (
        "the config must retain the accumulator the bytes were written to, "
        f"not build a fresh one per read; got {final.capture.total_bytes} bytes"
    )


def test_a_config_without_a_policy_owns_no_accumulator() -> None:
    """No policy means no sink, which is how a discarded stream is configured."""
    assert _config(None).capture_sink is None, (
        "no policy must mean no accumulator, not an empty one"
    )


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

    The payload is written in one chunk twice the bound, so the drop happens
    on the first read rather than depending on how the reader happens to
    partition a single ``feed_data``.
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
    assert result.capture is not None, (
        "a bounded drain must report a capture record; got None"
    )
    assert result.capture.truncated is True, (
        "a payload the bound cut must report truncation"
    )


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

    The bound is what this package of modules is about, so the no-bound case is
    stated here too: a caller who configured nothing must see the payload they
    always saw, with a record that reports nothing dropped.
    """
    payload = b"a complete stream\nwith two lines\n"
    result = _run_drain(payload, FullCapture())

    assert result.payload == payload.decode(), (
        f"the full policy must return the decoded stream; got {result.payload!r}"
    )
    assert result.capture is not None, (
        "a full drain must report a capture record; got None"
    )
    assert result.capture.total_bytes == len(payload), (
        f"the record must count every byte; got {result.capture.total_bytes}"
    )
    assert result.capture.dropped_bytes == 0, (
        f"a full capture drops nothing; got {result.capture.dropped_bytes}"
    )
    assert result.capture.truncated is False, (
        "a capture that dropped nothing must not report truncation"
    )
