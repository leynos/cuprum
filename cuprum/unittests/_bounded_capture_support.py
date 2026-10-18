"""Fixtures shared by the bounded-capture suites.

The retention suites ask three different questions — does the drain reach the
accumulator, do the invariants hold over generated input, and does each
boundary behave — but they all build the same stdout capture config and read
the same payload-and-record pair off a finalized sink. Building either inline
in three places is how the suites drift into testing subtly different
configurations.

``_Retained`` lives here for the same reason. It exposes the accumulator's own
payload rather than a slice reconstructed from the record's arithmetic, so a
caller using it is asserting something about what was retained rather than
about its own reconstruction.
"""

from __future__ import annotations

import dataclasses as dc
import io
import typing as typ

from cuprum._streams import _StreamConfig
from cuprum.echo_events import EchoStream

if typ.TYPE_CHECKING:
    from cuprum._capture_types import CaptureSink
    from cuprum.capture_policy import CapturedStream, CapturePolicy


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


@dc.dataclass(frozen=True, slots=True)
class _Retained:
    """What one bounded accumulation actually kept.

    The payload is the accumulator's own answer, not a slice reconstructed from
    the record's arithmetic. Those two used to be the same expression in the
    suffix property, which made its assertions statements about the
    reconstruction rather than about the bytes the accumulator returned: any
    accumulator at all would pass them so long as the record agreed with the
    slice taken from the input.
    """

    payload: str | bytes | None
    """The finalized payload, exactly as the accumulator reported it."""

    record: CapturedStream
    """The stream record finalized beside the payload."""

    def text(self) -> str:
        """Return the payload as text, refusing anything else.

        Returns
        -------
        str
            The accumulator's own decoded payload.
        """
        assert isinstance(self.payload, str), (
            f"a bounded text capture must finalize to text; got {self.payload!r}"
        )
        return self.payload

    def retained_bytes(self) -> bytes:
        """Return the accumulator's own retained bytes, recovered by re-encoding.

        A text accumulator retains bytes and decodes them, so re-encoding its
        own payload recovers exactly what it kept. That is deliberately not a
        slice of the input: taking the slice would make the assertions agree
        with any accumulator whose record happened to name that slice.

        Returns
        -------
        bytes
            The bytes behind the accumulator's own payload.
        """
        return self.text().encode("utf-8")
