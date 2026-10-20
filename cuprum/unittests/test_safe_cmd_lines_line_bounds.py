"""End-to-end ``SafeCmd.lines()`` behaviour under ``max_line_chars``.

The bounded-line contract has two halves. The accumulator's own unit
properties live in ``test_stream_line_boundaries.py``, and the direct drain
tests live in ``test_stream_drain_line_observation.py``. This module covers
what only a real child can show: that iteration delivers the child's output
fragmented but complete, that a reader slower than the child is carried by the
bounded queue's backpressure rather than by buffering, that short lines keep
their identity, and that ``max_line_chars=None`` restores one event per line.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import RunOutputOptions
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.lines import LineEvent
    from cuprum.sh import CommandResult, SafeCmd


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def test_lines_bound_newline_free_output_as_it_arrives(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A long newline-free line reaches ``lines()`` as bounded fragments.

    Before the bound existed, the drain concatenated every fragment of an
    unterminated line and rescanned the growing prefix per chunk: unbounded
    memory and quadratic CPU for a child that never terminates its line. The
    child here writes several megabytes with no newline at all while capture
    and echo are off, so the only retention is line observation's own. Every
    event must respect the bound, and their concatenation must still be the
    child's complete output --- the bound changes delivery, not content.
    """
    line_bound = 4096
    payload = "a" * (1 << 20) + "b" * (1 << 20)
    command = python_builder(
        "-c",
        "import sys; sys.stdout.write('a' * (1 << 20) + 'b' * (1 << 20))",
    )

    async def collect() -> tuple[list[LineEvent], CommandResult]:
        """Iterate the stream inside its context manager and retain the result."""
        events: list[LineEvent] = []
        async with command.lines(
            output=RunOutputOptions(
                capture=False,
                echo=False,
                max_line_chars=line_bound,
            ),
        ) as stream:
            events.extend([event async for event in stream])
            assert stream.result is not None, (
                "the completed stream must publish its CommandResult"
            )
            return events, stream.result

    events, result = asyncio.run(collect())

    assert events, "the child's output must reach the iterator, not be discarded"
    too_long = [len(event.text) for event in events if len(event.text) > line_bound]
    assert not too_long, (
        f"no event may exceed max_line_chars={line_bound}, got sizes {too_long!r}"
    )
    assert "".join(event.text for event in events) == payload, (
        "the fragments must reconstruct the child's complete unterminated line"
    )
    assert {event.stream for event in events} == {"stdout"}, (
        "an unterminated stdout line must be tagged stdout throughout"
    )
    assert result.ok is True, f"the child must complete successfully, got {result!r}"
    assert result.stdout is None, "capture=False must leave stdout unset"


@pytest.mark.timeout(120)
def test_lines_bound_survives_a_slow_reader_without_losing_output(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A reader slower than the child still receives every fragment in order.

    The child emits far more fragments than the driver's 256-event queue can
    hold, so reaching the end proves the queue's backpressure -- not an
    unbounded buffer -- is what carried them. A short pause per event makes
    the reader measurably slower than the pipe, which is the condition a
    bounded queue exists for.
    """
    from cuprum._line_stream import _LINE_QUEUE_CAPACITY

    line_bound = 16
    fragment_count = 4 * _LINE_QUEUE_CAPACITY
    command = python_builder(
        "-c",
        f"import sys; sys.stdout.write('x' * {line_bound * fragment_count})",
    )

    async def collect() -> tuple[list[str], CommandResult]:
        """Consume every fragment, pausing once per event."""
        texts: list[str] = []
        async with command.lines(
            output=RunOutputOptions(
                capture=False,
                echo=False,
                max_line_chars=line_bound,
            ),
        ) as stream:
            async for event in stream:
                texts.append(event.text)
                await asyncio.sleep(0)
            assert stream.result is not None, (
                "a backpressured stream must still publish its result"
            )
            return texts, stream.result

    texts, result = asyncio.run(collect())

    assert len(texts) == fragment_count, (
        f"expected {fragment_count} fragments over the queue capacity, got {len(texts)}"
    )
    assert "".join(texts) == "x" * (line_bound * fragment_count), (
        "every fragment must arrive exactly once and in arrival order"
    )
    assert result.ok is True, f"the child must complete successfully, got {result!r}"


def test_lines_bound_keeps_short_lines_intact_and_ordered(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Terminated lines below the bound are delivered whole and in order.

    The bound must not fragment what already fits: a line at exactly the bound
    is one event, and the events keep the order the child wrote them in.
    """
    command = python_builder(
        "-c",
        "import sys\n"
        "for name in ('first', 'second', 'third'):\n"
        "    print(name, flush=True)\n",
    )

    async def collect() -> list[str]:
        """Collect the decoded stdout lines."""
        async with command.lines(
            output=RunOutputOptions(
                capture=False,
                echo=False,
                max_line_chars=len("second"),
            ),
        ) as stream:
            return [event.text async for event in stream if event.stream == "stdout"]

    texts = asyncio.run(collect())

    assert texts == ["first", "second", "third"], (
        f"short terminated lines must stay whole and ordered, got {texts!r}"
    )


def test_lines_without_a_bound_publishes_a_long_line_once(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """``max_line_chars=None`` restores one event for an unterminated line."""
    length = 200_000
    command = python_builder("-c", f"import sys; sys.stdout.write('q' * {length})")

    async def collect() -> list[str]:
        """Collect the decoded stdout lines."""
        async with command.lines(
            output=RunOutputOptions(
                capture=False,
                echo=False,
                max_line_chars=None,
            ),
        ) as stream:
            return [event.text async for event in stream if event.stream == "stdout"]

    texts = asyncio.run(collect())

    assert texts == ["q" * length], (
        f"``None`` must deliver the whole line as one event, got {len(texts)} event(s)"
    )
