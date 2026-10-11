"""A producer's cleanup must not decide what the run reports, or leak its own.

The producer is caller code, so its ``aclose`` is caller code too, and cuprum
calls it *after* the run's outcome is already settled. That timing produces two
obligations that pull in opposite directions, and this module tests both.

The first is that the run's own ending must survive. A finalizer that fails on
the way in — an ``aclose`` that raises during the call, before it has returned
anything to schedule — must not be able to stand in for the failure the caller
is already seeing. Equally, the guard that stops that must not be widened to
swallow cancellation, because a run being torn down must not be converted into
one that completed cleanly.

The second is that cuprum must not leak the finalizer it has stopped waiting
for. The bounded window limits how long cuprum *waits*, not how long the
caller's cleanup runs, so a finalizer can outlive the run and fail afterwards.
Nothing is left to await it by then, so the exception has to be retrieved from
the task itself or the loop reports it against a producer cuprum is finished
with.

Bounding the wait at all is
:mod:`cuprum.unittests.test_stdin_finalizer_bound`'s subject; this module
covers what is owed to the caller and to the loop once that bound has expired.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

from cuprum._subprocess_stdin_errors import _stdin_source_error
from cuprum._subprocess_stdin_finalize import _finalize_stdin_source

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Matches the writer's settle window, and long enough that a bound failing to
# apply shows up as an unmistakable overrun rather than as scheduling noise.
_WINDOW_S = 0.25


class _RaisingOnEntryFinalizer:
    """A producer whose ``aclose`` raises instead of returning an awaitable.

    The hook is called *after* the run has decided its own outcome, so a
    failure here must not be able to stand in for the run's ending.
    """

    def __aiter__(self) -> cabc.AsyncIterator[bytes]:
        """Return this object's own iterator."""
        return self

    async def __anext__(self) -> bytes:
        """Never yield: the caller finalizes this producer explicitly."""
        raise StopAsyncIteration

    def aclose(self) -> cabc.Awaitable[None]:
        """Fail synchronously, the way a closed handle or bare state check would.

        Raises
        ------
        RuntimeError
            Always. A plain ``def`` rather than ``async def`` is the point: the
            failure happens during the *call* to ``aclose``, before any
            awaitable exists for cuprum to schedule.
        """
        msg = "producer already released its handle"
        raise RuntimeError(msg)


def test_an_aclose_that_raises_on_entry_does_not_replace_the_run_failure() -> None:
    """A finalizer that fails before it can be scheduled is not the run's ending.

    The helper runs after the run's outcome is already decided, so a cleanup
    fault must not take its place. Here the caller is ending on a producer
    failure, and a broken ``aclose`` — one that raises during the call rather
    than from the awaiting of it — would otherwise replace that error with its
    own and change what the caller is told went wrong.
    """

    async def scenario() -> tuple[str, str]:
        """Fail a run on a producer error and report what the caller saw."""
        finalizer = _RaisingOnEntryFinalizer()
        error = _stdin_source_error(RuntimeError("the producer's real failure"))
        try:
            raise error
        except type(error) as exc:
            # The writer's own shape: the run's failure is active while the
            # producer is finalized, so a cleanup fault can only replace it.
            await _finalize_stdin_source(typ.cast("typ.Any", finalizer))
            return type(exc).__name__, str(exc)
        msg = "the run's own failure did not propagate"
        raise AssertionError(msg)  # pragma: no cover

    name, message = asyncio.run(scenario())

    assert name == "StdinSourceError", (
        f"the producer's failure was reported as {name}; a finalizer that "
        "raised on entry replaced the run's own outcome"
    )
    assert "real failure" in message, (
        f"the run's failure text is missing from {message!r}; the caller was "
        "told about the cleanup rather than about the producer"
    )


class _CancellingOnEntryFinalizer:
    """A producer whose ``aclose`` signals cancellation rather than failing."""

    def __aiter__(self) -> cabc.AsyncIterator[bytes]:
        """Return this object's own iterator."""
        return self

    async def __anext__(self) -> bytes:
        """Never yield: the caller finalizes this producer explicitly."""
        raise StopAsyncIteration

    def aclose(self) -> cabc.Awaitable[None]:
        """Signal cancellation from the call itself.

        ``CancelledError`` is a ``BaseException``, so a guard written as
        ``suppress(Exception)`` leaves it alone while one written as
        ``suppress(BaseException)`` would swallow it. That distinction is the
        point: a cleanup guard that covers cancellation turns a run being torn
        down into one that appears to have finished.

        Raises
        ------
        asyncio.CancelledError
            Always.
        """
        raise asyncio.CancelledError


def test_cancellation_from_the_finalizer_is_not_absorbed_by_the_guard() -> None:
    """The invocation guard suppresses cleanup failures, not cancellation.

    The guard around calling ``aclose`` must not be widened to catch
    everything: ``CancelledError`` is the caller's control flow, and a run
    being torn down must not be converted into one that completed cleanly.
    Written as ``suppress(Exception)`` the guard already has the right scope —
    this is the control that fails if it is ever broadened to
    ``suppress(BaseException)``.
    """

    async def scenario() -> str:
        """Finalize a producer whose hook signals cancellation."""
        finalizer = _CancellingOnEntryFinalizer()
        try:
            await _finalize_stdin_source(typ.cast("typ.Any", finalizer))
        except asyncio.CancelledError:
            return "cancelled"
        return "completed"

    assert asyncio.run(scenario()) == "cancelled", (
        "the cleanup guard absorbed the finalizer's cancellation; the guard "
        "must suppress ordinary failures without covering CancelledError"
    )


class _AbsorbingLateFailingFinalizer:
    """A producer whose cleanup outlasts the window, absorbs, and then fails.

    This is the unwinding shape: the finalizer is asked to stop, keeps going,
    and *then* raises. The failure lands after the reclaim has already
    returned, where no statement of the calling helper runs again — so only a
    callback attached to the task itself can retrieve it.
    """

    def __init__(self) -> None:
        """Start with no failure recorded."""
        self.failed = False

    def __aiter__(self) -> cabc.AsyncIterator[bytes]:
        """Return this object's own iterator."""
        return self

    async def __anext__(self) -> bytes:
        """Never yield: the caller finalizes this producer explicitly."""
        raise StopAsyncIteration

    async def aclose(self) -> None:
        """Absorb the cancellation, finish the release, then fail.

        The suppression models a release that unwinds under its own guard —
        the documented cooperation limit — and the raise is what a release
        failing *after* it was asked to stop looks like. Nothing is left to
        await it once the window has closed.

        Raises
        ------
        RuntimeError
            Always fails, after absorbing the cancellation.
        """
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(_WINDOW_S * 4)
        self.failed = True
        msg = "release failed after being reclaimed"
        raise RuntimeError(msg)


def test_a_finalizer_that_fails_as_it_unwinds_leaves_no_unretrieved_error() -> None:
    """A late finalizer failure is retrieved, not reported against the loop.

    The window bounds how long cuprum *waits*; it does not stop the finalizer.
    A finalizer that absorbs the cancellation, finishes anyway, and then fails
    sets its exception after the reclaim has returned — at which point the
    helper is gone and only the task itself can still be told about it.

    Without that retrieval the loop reports an unretrieved task exception,
    blaming a producer cuprum has already stopped using for a failure that
    cannot reach anyone. The loop's handler is the witness, because that report
    is exactly what the retrieval prevents.
    """
    reported: list[str] = []

    async def scenario() -> bool:
        """Finalize an absorbing late-failing producer and let the loop settle."""
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda _loop, context: reported.append(str(context.get("message", context)))
        )
        finalizer = _AbsorbingLateFailingFinalizer()
        await _finalize_stdin_source(typ.cast("typ.Any", finalizer))
        # The finalizer is still running here, so the failure it is about to
        # raise has not happened yet. Waiting it out is what gives the
        # retrieval something to retrieve and the loop a chance to complain.
        await asyncio.sleep(_WINDOW_S * 8)
        return finalizer.failed

    assert asyncio.run(scenario()), (
        "the finalizer never reached its failure, so this test would pass "
        "vacuously; the wait must outlast the finalizer's own release"
    )
    assert not reported, (
        "the loop reported an unretrieved finalizer outcome, which is the "
        f"noise the retrieval exists to prevent: {reported}"
    )
