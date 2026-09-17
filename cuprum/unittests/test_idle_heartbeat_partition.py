"""Hypothesis byte-partition coverage for the drain's activity hook.

The idle heartbeat only knows a child is alive because the canonical drain
reports every non-empty read to it. That report has to be independent of
everything the drain does with the bytes afterwards: withholding capture,
disabling echo, a sink that rejects a chunk, a chunk that decodes to no text
at all, or a chunk that completes no line. These are properties over arbitrary
chunk partitions rather than examples, because the interesting cases are the
splits -- a multibyte character cut in half, a line ending landing alone in its
own chunk -- that a handwritten fixture would have to enumerate to find.

The pinned behaviour of the hook's run-owned lifecycle lives in
``test_idle_heartbeat.py`` and ``test_idle_heartbeat_execution.py``.
"""

from __future__ import annotations

import asyncio
import codecs
import io
import typing as typ

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum._streams import _drain, _split_complete_lines, _StreamConfig

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_PROPERTY_MAX_EXAMPLES = 24
_CHUNKS = st.lists(st.binary(min_size=1, max_size=6), min_size=1, max_size=6)
_ACTIVITY = "activity"


class _ActivityRecorder:
    """Record, in order, the activity reports and line callbacks of one drain."""

    def __init__(self) -> None:
        """Start with an empty event log."""
        self.events: list[str] = []

    def note_activity(self) -> None:
        """Record one raw-read activity report."""
        self.events.append(_ACTIVITY)

    def on_line(self, line: str) -> None:
        """Record one decoded line callback."""
        self.events.append(f"line:{line}")

    def activities(self) -> int:
        """Count the activity reports recorded so far."""
        return self.events.count(_ACTIVITY)

    def reads_lead_lines(self) -> bool:
        """Report whether every line callback was preceded by a raw read."""
        activities = 0
        lines = 0
        for event in self.events:
            if event == _ACTIVITY:
                activities += 1
            else:
                lines += 1
            if activities < lines:
                return False
        return True


class _ChunkedReader:
    """Stub reader returning queued chunks one per ``read`` call, then EOF."""

    def __init__(self, chunks: cabc.Sequence[bytes]) -> None:
        """Store the chunks for sequential ``read`` calls."""
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        """Return the next queued chunk, or empty bytes at EOF."""
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _RejectingSink:
    """Text-only sink that rejects every payload it is offered."""

    def write(self, payload: str) -> int:
        """Raise the failure a narrow-encoding sink produces."""
        msg = "sink cannot represent payload"
        raise UnicodeEncodeError("cp1252", payload, 0, len(payload), msg)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


def _reader(chunks: cabc.Sequence[bytes]) -> asyncio.StreamReader:
    """Build a stream-reader-shaped stub yielding the given chunks."""
    return typ.cast("asyncio.StreamReader", _ChunkedReader(chunks))


def _config(
    recorder: _ActivityRecorder,
    *,
    capture: bool = True,
    echo: bool = True,
    sink: typ.IO[str] | None = None,
) -> _StreamConfig:
    """Build a stream config whose only run-owned observer is *recorder*."""
    return _StreamConfig(
        capture_output=capture,
        echo_output=echo,
        sink=sink if sink is not None else io.StringIO(),
        encoding="utf-8",
        errors="replace",
        activity=recorder.note_activity,
    )


def _line_feeder(recorder: _ActivityRecorder) -> cabc.Callable[[bytes], None]:
    """Return a chunk observer emitting complete decoded lines."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""

    def feed(chunk: bytes) -> None:
        """Decode *chunk* and report every line it completes."""
        nonlocal pending
        complete, pending = _split_complete_lines(pending + decoder.decode(chunk))
        for line in complete:
            recorder.on_line(line)

    return feed


def _run_drain(
    recorder: _ActivityRecorder,
    chunks: cabc.Sequence[bytes],
    *,
    capture: bool = True,
    echo: bool = True,
) -> str | None:
    """Run the canonical drain over *chunks* and return what it captured."""
    config = _config(recorder, capture=capture, echo=echo)

    async def exercise() -> str | None:
        """Drain the chunked reader with the configuration under test."""
        return await _drain(_reader(chunks), config)

    return asyncio.run(exercise())


def _expected_text(chunks: cabc.Sequence[bytes]) -> str:
    """Return the text a UTF-8 drain of *chunks* must capture."""
    return b"".join(chunks).decode("utf-8", errors="replace")


_PROPERTY_SETTINGS = settings(
    max_examples=_PROPERTY_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)


@_PROPERTY_SETTINGS
@given(chunks=_CHUNKS)
def test_every_non_empty_chunk_reports_activity(chunks: list[bytes]) -> None:
    """Property: activity is reported once per raw read, whatever the split."""
    recorder = _ActivityRecorder()
    captured = _run_drain(recorder, chunks)
    assert captured == _expected_text(chunks), (
        f"capture must survive any chunking for chunks={chunks!r}, "
        f"captured={captured!r}"
    )
    assert recorder.activities() == len(chunks), (
        "each non-empty chunk must report exactly one activity for "
        f"chunks={chunks!r}, events={recorder.events!r}"
    )
    assert recorder.events[0] == _ACTIVITY, (
        "the first chunk must be reported before anything it produces for "
        f"events={recorder.events!r}"
    )


@_PROPERTY_SETTINGS
@given(chunks=_CHUNKS)
def test_activity_is_reported_before_every_line(chunks: list[bytes]) -> None:
    """Property: no line callback can precede the read that produced it."""
    recorder = _ActivityRecorder()

    async def exercise() -> str | None:
        """Drain with a line observer recording into the same event log."""
        return await _drain(
            _reader(chunks),
            _config(recorder),
            on_chunk=_line_feeder(recorder),
        )

    asyncio.run(exercise())
    assert recorder.reads_lead_lines(), (
        "every line callback must be preceded by the read that produced it for "
        f"chunks={chunks!r}, events={recorder.events!r}"
    )
    assert recorder.activities() == len(chunks), (
        "line decoding must not change how many reads are reported for "
        f"chunks={chunks!r}, events={recorder.events!r}"
    )


@_PROPERTY_SETTINGS
@given(chunks=_CHUNKS)
def test_observed_output_without_capture_stays_unretained(
    chunks: list[bytes],
) -> None:
    """Property: a run may watch for silence while retaining nothing."""
    recorder = _ActivityRecorder()
    captured = _run_drain(recorder, chunks, capture=False, echo=False)
    assert captured is None, (
        f"an observing run must retain no text for chunks={chunks!r}, got {captured!r}"
    )
    assert recorder.activities() == len(chunks), (
        "observation must still see every chunk for "
        f"chunks={chunks!r}, events={recorder.events!r}"
    )


@_PROPERTY_SETTINGS
@given(chunks=_CHUNKS)
def test_rejected_echo_chunks_still_report_activity(chunks: list[bytes]) -> None:
    """Property: a chunk the sink refuses still counts as the child talking."""
    recorder = _ActivityRecorder()
    config = _config(recorder, sink=typ.cast("typ.IO[str]", _RejectingSink()))

    async def exercise() -> str | None:
        """Drain with the sink that refuses every echoed chunk."""
        return await _drain(_reader(chunks), config)

    captured = asyncio.run(exercise())
    assert captured == _expected_text(chunks), (
        f"capture must complete despite a rejected echo for chunks={chunks!r}"
    )
    assert recorder.activities() == len(chunks), (
        "echo rejection must not hide activity for "
        f"chunks={chunks!r}, events={recorder.events!r}"
    )
