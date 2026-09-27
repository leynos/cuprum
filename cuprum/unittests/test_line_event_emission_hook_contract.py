"""The composed line callback's scheduling and failure contract.

These are the V3 cases that are *about* the per-line path, so they drive the
composed callback rather than the generic ``emit``. They cover what a caller
observes when lines are delivered: hook ordering, what a failing hook leaves
behind, and how a clock failure fails a delivery.

``_LineHookOutcome`` is an awaitable a caller-supplied ``on_line`` may return,
but the *composed* callback is a plain function: internally it calls
``observation.emit``, which schedules an async hook itself. That is why a hook
returning an awaitable still has its task retained on the observation's list
while the callback's own return value stays ``None``.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum._line_callbacks import _compose_line_callbacks, _LineEmissionContext
from cuprum._observability import _wait_for_exec_hook_tasks
from cuprum.unittests.test_line_event_emission_support import (
    _deliver,
    _make_observation,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent


class _LineEmissionHookError(Exception):
    """Raised by an observe hook to fail one delivered line."""


class _FatalLineEmissionHookError(BaseException):
    """A non-cancellation ``BaseException`` raised off a delivered line."""


class _ClockFailureError(Exception):
    """Raised by the wall-clock callable to fail emission before dispatch."""


class TestLineDeliveryHookContract:
    """Schedule ordering, failure retention, and clock-failure discipline.

    These are the V3 cases that are *about* the line path, so they drive the
    composed callback rather than the generic ``emit``. The dispatch-level
    contract — a later hook's failure preserving an earlier hook's scheduled
    task — is already pinned by ``test_cqrs_helpers``; what is new here is that
    the composed callback reaches that dispatch at all, and that its own
    per-line work neither leaks a clock read nor swallows a failure.

    ``_LineHookOutcome`` is an awaitable a caller-supplied ``on_line`` may
    return, but the *composed* callback is a plain function: internally it
    calls ``observation.emit``, which schedules an async hook itself. That is
    why a hook returning an awaitable still has its task retained on the
    observation's list while the callback's own return value stays ``None``.
    """

    def test_delivered_lines_reach_every_async_hook_in_order(self) -> None:
        """Each line reaches all async hooks, and hooks run in registration order."""

        async def run() -> list[tuple[str, str]]:
            """Deliver two lines and report each hook's view, in order."""
            trace: list[tuple[str, str]] = []

            def first(event: ExecEvent) -> cabc.Awaitable[None]:
                """Record this hook's view of the line, then yield."""

                async def record() -> None:
                    """Yield once, then record the first hook's view of the line."""
                    await asyncio.sleep(0)
                    trace.append(("first", typ.cast("str", event.line)))

                return record()

            def second(event: ExecEvent) -> cabc.Awaitable[None]:
                """Record this hook's view of the line, then yield."""

                async def record() -> None:
                    """Yield once, then record the second hook's view of the line."""
                    await asyncio.sleep(0)
                    trace.append(("second", typ.cast("str", event.line)))

                return record()

            observation = _make_observation((first, second))
            pending = observation.pending_tasks
            callback = _compose_line_callbacks(
                observation,
                _LineEmissionContext(
                    stream="stdout", pid=7, on_line=None, started_at=0.0
                ),
            )
            assert callback is not None, "two observe hooks must compose a callback"

            _deliver(callback, 2)
            scheduled_after_delivery = len(pending)
            await _wait_for_exec_hook_tasks(pending)
            assert len(pending) == 0, "cleanup must clear the retained tasks"
            assert scheduled_after_delivery == 4, (
                "each of the two lines must schedule one task per hook, got "
                f"{scheduled_after_delivery}"
            )
            return trace

        trace = asyncio.run(run())

        assert trace == [
            ("first", "line-0"),
            ("second", "line-0"),
            ("first", "line-1"),
            ("second", "line-1"),
        ], f"hooks must see every line in registration order, got {trace!r}"

    def test_later_hook_failure_preserves_the_scheduled_prefix(self) -> None:
        """A failing later hook leaves the earlier hook's task on the list."""

        async def run() -> tuple[list[asyncio.Task[None]], list[str]]:
            """Deliver one line into a failing hook and report what survived."""
            seen: list[str] = []

            def async_hook(event: ExecEvent) -> cabc.Awaitable[None]:
                """Record the line, then yield so the task is genuinely pending."""

                async def record() -> None:
                    """Yield once, then record the line for the surviving prefix."""
                    await asyncio.sleep(0)
                    seen.append(typ.cast("str", event.line))

                return record()

            def failing_hook(_event: ExecEvent) -> None:
                """Fail after the earlier hook has already scheduled its task.

                Raises
                ------
                _LineEmissionHookError
                    Always, to fail the delivery.
                """
                raise _LineEmissionHookError

            observation = _make_observation((async_hook, failing_hook))
            pending = observation.pending_tasks
            callback = _compose_line_callbacks(
                observation,
                _LineEmissionContext(
                    stream="stdout", pid=7, on_line=None, started_at=0.0
                ),
            )
            assert callback is not None, "two observe hooks must compose a callback"

            with pytest.raises(_LineEmissionHookError):
                callback("only-line")

            # The callback must not swallow the failure: the caller sees the
            # hook's own exception, not an emission wrapper.
            preserved = list(pending)
            assert len(preserved) == 1, (
                "the earlier hook's scheduled task must survive the failure, got "
                f"{len(preserved)}"
            )
            await _wait_for_exec_hook_tasks(pending)
            return preserved, seen

        preserved, seen = asyncio.run(run())

        assert len(preserved) == 1, "the scheduled prefix must be retained"
        assert seen == ["only-line"], (
            f"cleanup must await the preserved task to completion, got {seen!r}"
        )

    def test_non_cancellation_base_exception_also_preserves_the_prefix(self) -> None:
        """A fatal (non-cancellation) hook failure preserves the prefix too."""

        async def run() -> list[asyncio.Task[None]]:
            """Deliver into a hook raising a ``BaseException``."""
            completed: list[bool] = []

            def async_hook(_event: ExecEvent) -> cabc.Awaitable[None]:
                """Complete a marker once awaited."""

                async def record() -> None:
                    """Yield once, then mark the hook's awaited work as complete."""
                    await asyncio.sleep(0)
                    completed.append(True)

                return record()

            def fatal_hook(_event: ExecEvent) -> None:
                """Raise a non-cancellation ``BaseException``.

                Raises
                ------
                _FatalLineEmissionHookError
                    Always.
                """
                raise _FatalLineEmissionHookError

            observation = _make_observation((async_hook, fatal_hook))
            pending = observation.pending_tasks
            callback = _compose_line_callbacks(
                observation,
                _LineEmissionContext(
                    stream="stdout", pid=7, on_line=None, started_at=0.0
                ),
            )
            assert callback is not None, "two observe hooks must compose a callback"

            with pytest.raises(_FatalLineEmissionHookError):
                callback("only-line")

            preserved = list(pending)
            await _wait_for_exec_hook_tasks(pending)
            assert completed == [True], (
                "cleanup must still await the task the fatal hook displaced"
            )
            return preserved

        preserved = asyncio.run(run())

        assert len(preserved) == 1, (
            f"a base exception must preserve the scheduled prefix, got {len(preserved)}"
        )

    def test_clock_failure_fails_the_delivery_and_schedules_nothing(self) -> None:
        """A failing wall clock fails the line and adds no task to the list."""
        clock_calls = 0

        def clock() -> float:
            """Fail the way a broken clock source would.

            Raises
            ------
            _ClockFailureError
                Always.
            """
            nonlocal clock_calls
            clock_calls += 1
            raise _ClockFailureError

        async def async_hook(_event: ExecEvent) -> None:
            """Yield, so a scheduled task would be observable if one were made."""
            await asyncio.sleep(0)

        observation = _make_observation((async_hook,), clock=clock)
        pending = observation.pending_tasks
        callback = _compose_line_callbacks(
            observation,
            _LineEmissionContext(stream="stdout", pid=7, on_line=None, started_at=0.0),
        )
        assert callback is not None, "an observe hook must compose a callback"

        with pytest.raises(_ClockFailureError):
            callback("only-line")

        # The clock is read while the event is being built, so the failure
        # lands before dispatch: nothing was scheduled, and no half-built
        # prefix is left for a caller to settle.
        assert clock_calls == 1, (
            f"the failing clock must be read exactly once, got {clock_calls}"
        )
        assert pending == [], (
            f"a pre-dispatch failure must add no tasks, got {pending!r}"
        )

    def test_clock_failure_leaves_a_prior_lines_tasks_intact(self) -> None:
        """A clock failure on a later line does not discard an earlier prefix."""

        async def run() -> tuple[int, int, list[str]]:
            """Deliver a good line then a clock-failing one, and settle.

            Returns
            -------
            tuple[int, int, list[str]]
                The scheduled count after the first line, the count after the
                failure, and what the preserved task went on to deliver.
            """
            readings = iter([100.0])
            calls = 0

            def clock() -> float:
                """Answer once, then fail.

                Returns
                -------
                float
                    The single successful reading, on the first call.

                Raises
                ------
                _ClockFailureError
                    On every call after the first.
                """
                nonlocal calls
                calls += 1
                try:
                    return next(readings)
                except StopIteration:
                    raise _ClockFailureError from None

            seen: list[str] = []

            def async_hook(event: ExecEvent) -> cabc.Awaitable[None]:
                """Record the line, then yield so the task stays pending."""

                async def record() -> None:
                    """Yield once, then record the line before the clock fails."""
                    await asyncio.sleep(0)
                    seen.append(typ.cast("str", event.line))

                return record()

            observation = _make_observation((async_hook,), clock=clock)
            pending = observation.pending_tasks
            callback = _compose_line_callbacks(
                observation,
                _LineEmissionContext(
                    stream="stdout", pid=7, on_line=None, started_at=0.0
                ),
            )
            assert callback is not None, "an observe hook must compose a callback"

            # The first line must already have scheduled its hook task, and
            # that task must still be live — not yet awaited, not discarded —
            # across the second line's failure.
            callback("first-line")
            scheduled_after_first = len(pending)
            with pytest.raises(_ClockFailureError):
                callback("second-line")
            after_failure = len(pending)

            # Awaiting the survivor is the point: a discarded prefix could not
            # be settled at all, and a task cancelled by the failure would
            # raise instead of delivering the first line it was scheduled for.
            await _wait_for_exec_hook_tasks(pending)
            return scheduled_after_first, after_failure, seen

        scheduled_after_first, after_failure, seen = asyncio.run(run())

        assert scheduled_after_first == 1, (
            f"the first line must schedule its hook task, got {scheduled_after_first}"
        )
        assert after_failure == 1, (
            "a later line's clock failure must not discard the earlier prefix, "
            f"got {after_failure}"
        )
        assert seen == ["first-line"], (
            f"the preserved task must still deliver its own line, got {seen!r}"
        )
