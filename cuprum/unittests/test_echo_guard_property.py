"""Property coverage for the two per-write echo guards.

``test_stream_echo_guard.py`` owns the encode guard as examples, and
``test_broken_pipe_echo_guard.py`` owns the broken-pipe guard as examples.
Both guards are decided per *write*, though, so the exhaustive cases — every
payload, every byte partition, every write position that can fail — are out of
reach for examples: the drain stops echoing on the first rejection, so what a
property has to establish is that capture stays complete while no write
follows the rejection, whatever the partition and whichever write failed.

This module holds both properties so the two guards can be read side by side.
They share every line except the exception the sink raises, which is the whole
claim: the opt-in broken-pipe recovery and the unconditional encode recovery
differ in the condition that arms them and in nothing else.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum._streams import _drain, _RelayDiagnostics, _StreamConfig
from cuprum.echo_events import (
    BrokenPipePolicy,
    EchoErrorCategory,
    EchoStream,
    RelayFallback,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_PROPERTY_MAX_EXAMPLES = 24
# A 30-byte cap keeps every generated drain short, so the properties stay cheap
# while still admitting multibyte sequences split across chunk boundaries.
_PAYLOAD_MAX_CHARACTERS = 30
_LOGGER_NAME = "cuprum.stream"


def _split_at(payload: bytes, cut_points: cabc.Sequence[int]) -> tuple[bytes, ...]:
    """Split a payload at sorted, deduplicated cut points."""
    bounds = sorted({point for point in cut_points if 0 < point < len(payload)})
    pieces: list[bytes] = []
    start = 0
    for bound in bounds:
        pieces.append(payload[start:bound])
        start = bound
    pieces.append(payload[start:])
    return tuple(piece for piece in pieces if piece)


def _count_text_writes(chunks: tuple[bytes, ...]) -> int:
    """Count the non-empty text writes the drain's echo decoder will make."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    writes = 0
    for chunk in chunks:
        if decoder.decode(chunk):
            writes += 1
    if decoder.decode(b"", final=True):
        writes += 1
    return writes


@st.composite
def _write_counting_case(
    draw: st.DrawFn,
) -> tuple[bytes, tuple[bytes, ...], int]:
    """Generate a UTF-8 payload, a byte partition, and a failing write index.

    Draws a non-empty payload, cuts it at arbitrary byte offsets so a partition
    may split a multibyte sequence across chunks exactly as a real pipe read
    would, and then draws the 1-based sink write that must fail. The failing
    position is bounded by the exact number of text writes the partition
    produces, so the failure always lands on a write that really happens:
    ``_write_chunk`` writes only when the decoder yields text, and always
    flushes, so counting writes rather than chunks is what keeps the index
    inside the range echo is still enabled for.

    Returns
    -------
    tuple[bytes, tuple[bytes, ...], int]
        The complete UTF-8 payload, its byte-chunk partition, and the 1-based
        sink write that must raise.
    """
    payload = draw(
        st.text(min_size=1, max_size=_PAYLOAD_MAX_CHARACTERS).map(str.encode),
    )
    # A one-byte payload has no interior cut point, so the element bound is
    # clamped to stay constructible; the empty list it admits is the
    # single-chunk partition, which is a real case rather than a dodge.
    cut_points = draw(
        st.lists(
            st.integers(min_value=1, max_value=max(len(payload) - 1, 1)),
            min_size=0,
            max_size=min(8, len(payload) - 1),
            unique=True,
        ),
    )
    chunks = _split_at(payload, cut_points)
    return (
        payload,
        chunks,
        draw(
            st.integers(min_value=1, max_value=_count_text_writes(chunks)),
        ),
    )


class _ChunkedReader:
    """Stub stream reader yielding queued chunks before EOF."""

    def __init__(self, chunks: cabc.Sequence[bytes]) -> None:
        """Store chunks for sequential ``read`` calls."""
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        """Return the next queued chunk, or empty bytes at EOF."""
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FailingWriteSink:
    """Recording sink failing at one chosen write with a given exception.

    ``error`` is a factory rather than an instance because the traceback of a
    single exception object would be rewritten on every raise, and the sharp
    assertion below — that no write follows the first rejection — is easier to
    trust when each attempt raises a fresh error.
    """

    def __init__(
        self,
        fail_on_write: int,
        error: cabc.Callable[[], BaseException],
    ) -> None:
        """Store the 1-based write index that must fail, and how it fails."""
        self._fail_on_write = fail_on_write
        self._error = error
        self.attempts = 0
        self.rejected_attempt: int | None = None

    def write(self, payload: str) -> int:
        """Record the attempt and fail once the index is reached."""
        self.attempts += 1
        if self.rejected_attempt is not None:
            msg = "the echo guard must stop further writes"
            raise AssertionError(msg)
        if self.attempts >= self._fail_on_write:
            self.rejected_attempt = self.attempts
            raise self._error()
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


def _config(
    sink: typ.IO[str],
    policy: BrokenPipePolicy,
) -> _StreamConfig:
    """Build a UTF-8 stream config that echoes into *sink* under *policy*."""
    return _StreamConfig(
        capture_output=True,
        echo_output=True,
        sink=sink,
        encoding="utf-8",
        errors="replace",
        broken_pipe_policy=policy,
    )


async def _drain_case(
    case: tuple[bytes, tuple[bytes, ...], int],
    error: cabc.Callable[[], BaseException],
    policy: BrokenPipePolicy,
) -> tuple[str | None, _FailingWriteSink, tuple[RelayFallback, ...]]:
    """Drain one generated partition into a sink that fails at one write."""
    _payload, chunks, failing_write = case
    sink = _FailingWriteSink(failing_write, error)
    relay_diagnostics = _RelayDiagnostics()
    captured = await _drain(
        typ.cast("asyncio.StreamReader", _ChunkedReader(chunks)),
        _config(typ.cast("typ.IO[str]", sink), policy),
        relay_diagnostics=relay_diagnostics,
    )
    relay_diagnostics.settle()
    return captured, sink, relay_diagnostics.snapshot()


@pytest.mark.parametrize(
    ("policy", "error", "category"),
    [
        pytest.param(
            BrokenPipePolicy.BEST_EFFORT,
            lambda: BrokenPipeError("closed presentation destination"),
            EchoErrorCategory.BROKEN_PIPE,
            id="best-effort-broken-pipe",
        ),
        pytest.param(
            BrokenPipePolicy.STRICT,
            lambda: UnicodeEncodeError(
                "cp1252",
                "x",
                0,
                1,
                "character maps to <undefined>",
            ),
            EchoErrorCategory.UNICODE_ENCODE,
            id="encode-guard-always-armed",
        ),
    ],
)
@settings(
    max_examples=_PROPERTY_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(case=_write_counting_case())
def test_echo_guard_stops_after_the_first_write_failure(
    case: tuple[bytes, tuple[bytes, ...], int],
    caplog: pytest.LogCaptureFixture,
    policy: BrokenPipePolicy,
    error: cabc.Callable[[], BaseException],
    category: EchoErrorCategory,
) -> None:
    """Property: the first failed write disables echo exactly once per drain.

    For every payload, partition, and failing write position, capture must
    decode the whole payload, the failing write must be the drain's last echo
    attempt, and exactly one wired diagnostic must describe the transition —
    one structured warning, and one result record naming the category.

    The two parametrisations are the claim above: the encode guard recovers
    under the default ``STRICT`` policy because it is not opt-in, and the
    broken-pipe guard recovers under ``BEST_EFFORT``. Everything else in the
    body is shared, so a divergence in either guard's behaviour shows up as
    one arm failing while the other passes.
    """
    payload, chunks, failing_write = case

    # Hypothesis reuses the function-scoped fixture across examples, so the
    # records of every earlier example are still here; clear them before the
    # drain that this example asserts on.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        captured, sink, fallbacks = asyncio.run(_drain_case(case, error, policy))

    context = f"chunks={chunks!r}, failing_write={failing_write!r}"
    assert captured == payload.decode("utf-8", errors="replace"), (
        f"capture must decode the complete payload for {context}"
    )
    assert sink.rejected_attempt is not None, (
        f"the sink must reject one of the chunk writes for {context}"
    )
    assert sink.attempts == sink.rejected_attempt, (
        f"no write may follow the first rejection for {context}, "
        f"attempts={sink.attempts!r}, rejected={sink.rejected_attempt!r}"
    )

    warnings = [record for record in caplog.records if record.name == _LOGGER_NAME]
    assert len(warnings) == 1, (
        f"exactly one disable warning must be logged for {context}, "
        f"warnings={warnings!r}"
    )
    record = warnings[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "echo_disabled_stream_rejected_output"
    assert record.exc_info is None, (
        "the handled sink failure must not carry the original exception: "
        f"exc_info={record.exc_info!r}"
    )
    fields = vars(record)
    assert fields["cuprum_operation"] == "echo_chunk"
    assert fields["cuprum_stream"] == "stdout"
    assert fields["cuprum_transition"] == "echo_disabled"
    assert fields["cuprum_error_category"] == category.value
    assert fallbacks == (
        RelayFallback(stream=EchoStream.STDOUT, error_category=category),
    ), f"exactly one result record must describe the transition for {context}"
