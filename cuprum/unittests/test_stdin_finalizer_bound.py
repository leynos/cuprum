"""A producer's cleanup must not hold the run — or the pipe — open.

The producer is caller code, so its ``aclose()`` is caller code too. Two things
follow, and this module tests both.

The wait is bounded: a finalizer that suspends cannot keep the run pending past
its own deadline. ``wait_for`` cannot deliver that, because it waits for the
cancellation it requests to take effect, so a finalizer that absorbs
``CancelledError`` while finishing its release is not bounded by it at all —
the window expires and the call still blocks.

The pipe is released first: cuprum closes its own stdin before it finalizes the
producer, so whatever the caller's cleanup does, the child's EOF is already
sent. Finalizing first let a stalled finalizer hold the child's input open.

Every record is read inside the loop. An abandoned async generator is closed by
``shutdown_asyncgens`` when the loop tears down, so a record read after
``asyncio.run`` returns would show a finalized producer even for a run that
never finalized it.
"""

from __future__ import annotations

import asyncio
import sys
import time
import typing as typ

import pytest

from cuprum import _subprocess_stdin_finalize as finalize
from cuprum._subprocess_stdin_finalize import _finalize_stdin_source
from cuprum._subprocess_stdin_stream import _stdin_codec, _write_stdin_stream
from cuprum.sh import ExecutionContext, StdinStream
from cuprum.unittests.test_line_event_emission_support import _make_observation

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Long enough that the bound is unmistakable against scheduling noise, short
# enough that a failure to bound shows up as a fast test rather than a hang.
_FINALIZER_STALL_S = 5.0
_WINDOW_S = 0.25  # matches _STDIN_SETTLE_GRACE_S


class _SuspendingFinalizer:
    """A producer whose cleanup suspends, and records how it was reclaimed."""

    def __init__(self, *, absorbs_cancellation: bool) -> None:
        """Choose whether cleanup finishes or ignores the cancellation."""
        self.absorbs_cancellation = absorbs_cancellation
        self.finalize_started = asyncio.Event()
        self.finalize_finished = asyncio.Event()

    def __aiter__(self) -> cabc.AsyncIterator[bytes]:
        """Return this object's own iterator."""
        return self

    async def __anext__(self) -> bytes:
        """Never yield: the caller finalizes this producer explicitly."""
        raise StopAsyncIteration

    async def aclose(self) -> None:
        """Suspend in cleanup, optionally absorbing the cancellation.

        The absorbing variant models a realistic finalizer: it holds a
        ``try``/``except`` around the release it is performing. That is the
        shape ``wait_for`` cannot bound.

        Raises
        ------
        asyncio.CancelledError
            When the non-absorbing variant is cancelled. That variant re-raises
            so the caller of ``aclose`` sees the cancellation; the absorbing
            variant swallows it deliberately.
        """
        self.finalize_started.set()
        try:
            await asyncio.sleep(_FINALIZER_STALL_S)
        except asyncio.CancelledError:
            if not self.absorbs_cancellation:
                raise
            # Keeps working through the release, as caller cleanup may.
            await asyncio.sleep(_FINALIZER_STALL_S)
        self.finalize_finished.set()


def test_a_suspending_finalizer_is_bounded() -> None:
    """Cleanup that suspends does not keep the caller waiting for it.

    The finalizer sleeps far past the window. If the wait were unbounded — or
    bounded by ``wait_for``, which cannot bound a finalizer that absorbs
    cancellation — the call below would not return until the finalizer chose
    to finish.
    """

    async def scenario() -> float:
        """Finalize a suspending producer and report how long the wait took."""
        finalizer = _SuspendingFinalizer(absorbs_cancellation=False)
        started = time.monotonic()
        await _finalize_stdin_source(typ.cast("typ.Any", finalizer))
        elapsed = time.monotonic() - started
        assert finalizer.finalize_started.is_set(), (
            "the finalizer must have been entered, or the bound below is a "
            "statement about a call that never happened"
        )
        return elapsed

    elapsed = asyncio.run(scenario())

    assert elapsed < _FINALIZER_STALL_S / 2, (
        f"finalization took {elapsed:.2f}s; the {_WINDOW_S}s window must bound "
        "a finalizer that suspends rather than waiting it out"
    )


def test_a_finalizer_absorbing_cancellation_is_still_bounded() -> None:
    """The window bounds a finalizer that catches cancellation and continues.

    This is the case ``wait_for`` cannot handle, and the reason the wait is
    ``asyncio.wait``. The finalizer ignores the request to stop and keeps
    sleeping; the caller must return anyway.

    The bound is therefore on cuprum's *wait*, not on the finalizer's work:
    the cleanup continues in the background and the loop reclaims it at
    shutdown. That is the cooperation limit, and it is what this asserts —
    a promise that the finalizer would be stopped would be one cuprum cannot
    keep.
    """

    async def scenario() -> tuple[float, bool]:
        """Time the wait on an absorbing producer and report if it finished."""
        finalizer = _SuspendingFinalizer(absorbs_cancellation=True)
        started = time.monotonic()
        await _finalize_stdin_source(typ.cast("typ.Any", finalizer))
        return time.monotonic() - started, finalizer.finalize_finished.is_set()

    elapsed, finished = asyncio.run(scenario())

    assert elapsed < _FINALIZER_STALL_S / 2, (
        f"finalization took {elapsed:.2f}s against a finalizer that absorbed "
        "cancellation; the wait is not bounded by its own window"
    )
    assert not finished, (
        "the finalizer absorbed cancellation and kept working, so its cleanup "
        "cannot have completed within the window; a completed record here "
        "means the test is not exercising the absorbing path at all"
    )


def test_a_fast_finalizer_is_awaited_to_completion() -> None:
    """A finalizer that finishes inside the window is not truncated by it.

    The bound exists for cleanup that would otherwise hang the run, so it must
    not become a deadline that abandons ordinary cleanup. This is the control
    that keeps the two bounded-path tests from describing a policy of
    cancelling every finalizer.
    """

    class _QuickFinalizer:
        """A producer whose cleanup completes immediately."""

        def __init__(self) -> None:
            """Start with no finalization recorded."""
            self.finalized = False

        def __aiter__(self) -> cabc.AsyncIterator[bytes]:
            """Return this object's own iterator."""
            return self

        async def __anext__(self) -> bytes:
            """Never yield: the caller finalizes this producer explicitly."""
            raise StopAsyncIteration

        async def aclose(self) -> None:
            """Record finalization, yielding once to stay a genuine await."""
            await asyncio.sleep(0)
            self.finalized = True

    async def scenario() -> bool:
        """Finalize a prompt producer and report whether its cleanup ran."""
        quick = _QuickFinalizer()
        await _finalize_stdin_source(typ.cast("typ.Any", quick))
        return quick.finalized

    assert asyncio.run(scenario()), (
        "a finalizer that finishes within the window must run to completion; "
        "cancelling it would abandon cleanup the run could afford to wait for"
    )


def test_a_finalizer_without_aclose_is_tolerated() -> None:
    """A plain async iterable with no ``aclose`` is left alone, not refused."""

    class _NoAclose:
        """A producer whose iterator exposes no cleanup hook."""

        def __aiter__(self) -> cabc.AsyncIterator[bytes]:
            """Return this object's own iterator."""
            return self

        async def __anext__(self) -> bytes:
            """Never yield."""
            raise StopAsyncIteration

    async def scenario() -> None:
        """Finalize a producer with no cleanup hook, which must not raise."""
        await _finalize_stdin_source(typ.cast("typ.Any", _NoAclose()))

    asyncio.run(scenario())


def test_the_settle_window_is_the_one_the_writer_uses() -> None:
    """The finalizer bound is the writer's settle window, not a second copy.

    Two independent constants would drift, and a finalizer given longer than
    the writer's own reclaim window would reintroduce the dependency the
    ordering exists to remove.
    """
    assert finalize._STDIN_SETTLE_GRACE_S == _WINDOW_S, (
        "the finalizer's window has drifted from the stdin writer's settle "
        f"grace ({finalize._STDIN_SETTLE_GRACE_S}); update this test's "
        "expectation only alongside that constant"
    )


@pytest.mark.parametrize("absorbs", [False, True])
def test_cancellation_during_the_wait_propagates(absorbs: bool) -> None:
    """Cancelling the run is not absorbed by the finalizer's window.

    Cancellation is the caller's control flow. A finalizer that absorbs it
    cannot turn a cancelled run into a completed one, which is what an
    unguarded wait would allow.

    The assertion is on the record rather than on the finalizer having
    stopped: the cancellation propagates outward either way, while whether the
    cleanup notices is the caller's business, not cuprum's.
    """

    async def scenario() -> str:
        """Cancel the wait and report whether the cancellation propagated."""
        finalizer = _SuspendingFinalizer(absorbs_cancellation=absorbs)
        task = asyncio.ensure_future(
            _finalize_stdin_source(typ.cast("typ.Any", finalizer))
        )
        await finalizer.finalize_started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "completed"

    assert asyncio.run(scenario()) == "cancelled", (
        "the run's cancellation must propagate; a finalizer's window must not "
        "convert it into a completed finalization"
    )


class _FinalizerObservingPipe:
    """A producer that records the pipe's state at the moment cleanup starts.

    The ordering under test is only observable from inside the caller's own
    finalizer: that is the one vantage point where both cuprum's pipe and the
    producer's cleanup are live at once.
    """

    def __init__(self, stdin: asyncio.StreamWriter) -> None:
        """Record the writer whose state cleanup will inspect."""
        self.stdin = stdin
        self.pipe_released_at_cleanup: bool | None = None

    def __aiter__(self) -> cabc.AsyncIterator[bytes]:
        """Return this object's own iterator."""
        return self

    async def __anext__(self) -> bytes:
        """Yield nothing: the child's EOF must arrive from the close."""
        raise StopAsyncIteration

    async def aclose(self) -> None:
        """Record whether the pipe was already closing when cleanup began."""
        self.pipe_released_at_cleanup = self.stdin.is_closing()


async def _run_with_observing_finalizer() -> _FinalizerObservingPipe:
    """Drive the streaming writer to completion with an observing producer."""

    async def main() -> _FinalizerObservingPipe:
        """Write to a real child and return the producer's own record."""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.stdin.read()",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdin = process.stdin
        assert stdin is not None, "the child was started with a stdin pipe"
        producer = _FinalizerObservingPipe(stdin)
        try:
            await _write_stdin_stream(
                process,
                StdinStream(chunks=producer),
                _stdin_codec(ExecutionContext()),
                _make_observation(),
            )
            await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        return producer

    return await main()


def test_the_pipe_is_released_before_the_producer_is_finalized() -> None:
    """Cuprum closes its own stdin before it waits on caller cleanup.

    The child reads to EOF, so its exit depends on cuprum's close reaching the
    pipe. Finalizing the producer first made that close wait on caller code:
    a finalizer that stalled held the child's input open for as long as its
    cleanup chose. The record is read from inside the finalizer, which is the
    only place both are live, and inside the loop, because the pipe's state
    after ``asyncio.run`` would describe a torn-down transport rather than
    this ordering.
    """
    producer = asyncio.run(_run_with_observing_finalizer())

    assert producer.pipe_released_at_cleanup is True, (
        "the stdin pipe was still open when the producer's cleanup began; "
        "cuprum's own resource must be released before caller code is awaited"
    )
