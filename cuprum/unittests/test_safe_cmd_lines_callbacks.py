"""Callback, queue and backpressure contracts for line observation."""

from __future__ import annotations

import asyncio
import time
import types
import typing as typ

from cuprum import RunOutputOptions

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum.lines import LineEvent
    from cuprum.sh import SafeCmd


def test_run_on_line_observes_without_capture_or_echo(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """``on_line`` alone keeps both pipes open when capture and echo are off.

    Without it, stdout and stderr are attached to ``DEVNULL`` and the callback
    would silently never fire.
    """
    events: list[LineEvent] = []
    command = python_builder(
        "-c",
        "import sys; print('out'); print('err', file=sys.stderr)",
    )

    result = asyncio.run(
        command.run(
            output=RunOutputOptions(capture=False, echo=False, on_line=events.append),
        )
    )

    observed = {(event.stream, event.text) for event in events}
    assert observed == {("stdout", "out"), ("stderr", "err")}, (
        f"on_line must observe both streams with capture and echo off, got {observed!r}"
    )
    assert result is not None, "run() must return a completed command result"
    assert result.stdout is None, "capture=False must leave stdout unset"
    assert result.stderr is None, "capture=False must leave stderr unset"


def test_line_event_queue_is_finite() -> None:
    """The driver's queue is bounded, so retention cannot grow without limit."""
    from cuprum._line_stream import _LINE_QUEUE_CAPACITY, _line_event_queue

    assert _LINE_QUEUE_CAPACITY > 0, "a bounded queue needs a positive capacity"
    assert _line_event_queue().maxsize == _LINE_QUEUE_CAPACITY, (
        "the driver must consume the bounded queue, not an unbounded one"
    )


def test_lines_stamp_events_from_the_observation_clock(
    monkeypatch: pytest.MonkeyPatch,
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Line stamps, start reference, and deadline all read one clock.

    One injected clock serves the whole run: its first reading becomes the
    start reference, and every later reading the stamp and the post-exit
    deadline are computed against. Two things are asserted, and neither is
    reachable by a coincidence of the machine's own uptime:

    * the stamp is the injected elapsed time, which can only hold if the start
      reference and the stamping read came from the same callable;
    * the run completes inside its deadline. The injected origin sits far
      above the machine's uptime, so a start taken from the real clock would
      compute an elapsed time larger than the whole timeout, which the
      post-exit drain reports as ``TimeoutExpired``.

    A stamp read from some *other* clock fails the first assertion instead: it
    subtracts the injected origin from a real reading, which is negative and
    therefore floors to ``0.0``. So each of the two ways to split the clock —
    a start from elsewhere, or a stamp from elsewhere — fails on its own.

    The clock is installed by replacing ``_command_internals.time``, the
    construction site that resolves the observation's ``monotonic_clock``,
    with a namespace of this test's own. Patching ``perf_counter`` on the
    standard-library module instead would reach every caller in the process:
    a start or stamp read directly from ``time.perf_counter()`` would receive
    the injected value too, so the split-clock regression this test exists to
    catch would pass. Patching the ``_StageObservation`` field default would
    not reach the run either: it binds at class creation, before any test
    patches anything.
    """
    from cuprum import _command_internals

    # Deliberately above any real ``perf_counter`` reading (the machine's
    # uptime, in seconds), so a stray real reading cannot be mistaken for the
    # injected one: a real stamp reads *below* this origin and floors to
    # ``0.0``, and a real deadline read leaves no time at all.
    clock_origin = 1e12
    readings = iter([clock_origin])

    def observation_clock() -> float:
        """Answer the injected start once, then the injected "now"."""
        return next(readings, clock_origin + 3.25)

    monkeypatch.setattr(
        _command_internals,
        "time",
        types.SimpleNamespace(perf_counter=observation_clock, time=time.time),
    )
    command = python_builder("-c", "print('clock')")

    async def collect() -> tuple[list[LineEvent], object]:
        """Iterate the command, reporting its events and its result."""
        stream = command.lines(timeout=30.0)
        events = [event async for event in stream]
        return events, stream.result

    events, result = asyncio.run(collect())

    assert [event.at for event in events] == [3.25], (
        "line timestamps must be measured from the observation clock the "
        f"start reference was sampled from, got {events!r}"
    )
    assert result is not None, (
        "a run whose post-exit drain stays inside its deadline must complete "
        "rather than report an expiry"
    )


def test_queue_line_sink_applies_backpressure() -> None:
    """A full queue parks the sink rather than raising or dropping the event."""
    from cuprum._line_stream import _line_event_queue, _queue_line_sink
    from cuprum.lines import LineEvent

    async def exercise() -> None:
        """Fill the queue, then confirm the sink waits for a free slot."""
        queue = _line_event_queue()
        sink = _queue_line_sink(queue)
        for index in range(queue.maxsize):
            queue.put_nowait(
                LineEvent(stream="stdout", at=0.0, text=str(index)),
            )

        parked = sink(LineEvent(stream="stdout", at=0.0, text="overflow"))
        assert parked is not None, (
            "the queue sink applies backpressure, so it answers with an awaitable"
        )
        blocked = asyncio.ensure_future(parked)
        await asyncio.sleep(0)
        assert not blocked.done(), (
            "a full queue must hold the sink instead of raising QueueFull"
        )

        drained = queue.get_nowait()
        assert isinstance(drained, LineEvent), "only line events are queued"
        assert drained.text == "0", "the queue drains in arrival order"
        await blocked
        assert queue.qsize() == queue.maxsize, (
            "the parked event must be delivered once a slot frees up"
        )

    asyncio.run(exercise())


def test_emit_line_awaits_an_asynchronous_sink() -> None:
    """A sink returning an awaitable holds the drain loop until it settles.

    That is the seam ``lines()`` relies on: the bounded queue can only push
    back on the child if the reader waits for the sink, because one read can
    carry thousands of lines.
    """
    from cuprum._streams import _emit_line

    started = asyncio.Event()
    released = asyncio.Event()

    async def sink(line: str) -> None:
        """Signal that the sink was entered, then wait to be released."""
        assert line == "line", f"the sink must receive the decoded line, got {line!r}"
        started.set()
        await released.wait()

    async def exercise() -> None:
        """Confirm the drain loop is held while the sink is unsettled."""
        pending = asyncio.ensure_future(_emit_line(sink, "line"))
        await started.wait()
        assert not pending.done(), "the drain loop must wait for the sink"
        released.set()
        await pending

    asyncio.run(exercise())


def test_emit_line_invokes_a_synchronous_sink() -> None:
    """A sink returning ``None`` is called without being awaited."""
    from cuprum._streams import _emit_line

    seen: list[str] = []

    async def exercise() -> None:
        """Emit one line through a plain callback."""
        await _emit_line(seen.append, "line")

    asyncio.run(exercise())

    assert seen == ["line"], "a synchronous sink must still receive every line"
