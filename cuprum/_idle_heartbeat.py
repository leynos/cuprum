"""Run-owned idle heartbeats for quiet children.

A child that produces no output for a whole interval is indistinguishable from
one that has wedged: the motivating case is a slow ``cargo publish --dry-run``
whose silence was read as a hang. This module turns that silence into a
bounded, parent-side keepalive — one line after ``idle_after`` seconds without
a non-empty read on any monitored stream, and one more on every further
interval, reset by any output at all.

Nothing here infers *why* a child is quiet. A heartbeat reports the absence of
observed output, not missing CPU time, a deadlock, or a healthy child; it never
terminates a process, never extends a timeout, and never feeds a child's
streams. The built-in diagnostic goes to the parent's stderr sink, so capture,
line observers, and the activity tracker itself cannot see it.

The state machine (:class:`_IdleSchedule`) is separate from emission
(:class:`_IdleNotifier`) and from asyncio (:meth:`_IdleMonitor.run`), so a test
drives the whole lifecycle from an injected clock without sleeping. Rendering a
keepalive line and reporting a failed one live in ``cuprum._idle_diagnostic``;
this module owns the timing contract, option validation, and the watchdog's
lifecycle.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import inspect
import logging
import math
import time
import typing as typ

from cuprum._idle_diagnostic import (
    _IdleDiagnostic,
    _LiveSink,
    _report_callback_result,
    _report_notification_failure,
)
from cuprum._streams import _MirrorCursor

if typ.TYPE_CHECKING:
    import collections.abc as cabc


_LOGGER = logging.getLogger("cuprum.idle")


@dc.dataclass(slots=True)
class _IdleSchedule:
    """The idle timestamps of one started run."""

    interval: float
    started_at: float
    last_activity: float
    next_deadline: float
    is_stopped: bool = False

    @classmethod
    def start(cls, interval: float, now: float) -> _IdleSchedule:
        """Begin timing at *now*, with the first deadline one interval out."""
        return cls(
            interval=interval,
            started_at=now,
            last_activity=now,
            next_deadline=now + interval,
        )

    def note_activity(self, now: float) -> None:
        """Record child output observed at *now* and push the deadline out."""
        if self.is_stopped:
            return
        self.last_activity = now
        self.next_deadline = now + self.interval

    def seconds_until_due(self, now: float) -> float:
        """Return how long to wait before the next notification is due."""
        return max(0.0, self.next_deadline - now)

    def take_due(self, now: float) -> tuple[float, float] | None:
        """Claim a notification that is due at *now*, or return ``None``.

        Returns
        -------
        tuple[float, float] | None
            The elapsed total and idle ages in seconds, or ``None`` when no
            notification is due: the run is stopped, or activity has already
            moved the deadline past *now*.
        """
        if self.is_stopped or now < self.next_deadline:
            return None
        # The next deadline is measured from this emission rather than from the
        # one it replaces: a loop that ran late emits once and schedules the
        # following interval instead of replaying the ticks it missed.
        self.next_deadline = now + self.interval
        return now - self.started_at, now - self.last_activity

    def stop(self) -> None:
        """Stop timing; idempotent, and no notification follows it."""
        self.is_stopped = True


@dc.dataclass(slots=True)
class _IdleNotifier:
    """Emit one idle notification, preferring the caller's own callback.

    A caller callback replaces the built-in renderer rather than running beside
    it. An ordinary failure disables idle notifications for the rest of the run
    and reports one sanitized warning: a run must not fail because its *silence
    reporting* failed, and a diagnostic that raised on every interval would
    bury the live log it exists to keep legible.

    A callback that *returns a value* is silenced on the same terms. It has
    already broken the synchronous, ``None``-returning contract, so repeating
    the report on every further interval would bury the log for exactly the
    same reason -- the one report is the whole diagnosis.

    Cancellation, ``KeyboardInterrupt``, and ``SystemExit`` are not ordinary
    failures and are never converted here; they escape this call unchanged.
    """

    callback: cabc.Callable[[float, float], None] | None
    diagnostic: _IdleDiagnostic

    def __call__(self, elapsed_total: float, elapsed_idle: float) -> bool:
        """Emit one notification; return whether more may follow."""
        try:
            return self._emit(elapsed_total, elapsed_idle)
        except Exception as exc:  # ruff: ignore[blind-except] - the report is sanitized by design: only the error type is named.
            _report_notification_failure(type(exc).__name__)
            return False

    def _emit(self, elapsed_total: float, elapsed_idle: float) -> bool:
        """Render the built-in diagnostic or invoke the caller's callback.

        Returns
        -------
        bool
            Whether further notifications may follow. Only a callback that
            returned a value ends the channel; the built-in renderer and a
            callback returning ``None`` both leave it running.
        """
        callback = self.callback
        if callback is None:
            self.diagnostic.write(
                elapsed_total=elapsed_total,
                elapsed_idle=elapsed_idle,
            )
            return True
        result = typ.cast("object", callback(elapsed_total, elapsed_idle))
        if result is not None:
            _report_callback_result(result)
            return False
        return True


@dc.dataclass(slots=True)
class _IdleMonitor:
    """Run-owned idle heartbeat: schedule, emission, mirror, and driver."""

    interval: float
    notify: _IdleNotifier
    mirror: _MirrorCursor
    clock: cabc.Callable[[], float] = time.monotonic
    schedule: _IdleSchedule | None = None
    task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether the run is armed and not yet stopped."""
        schedule = self.schedule
        return schedule is not None and not schedule.is_stopped

    def launch(self) -> None:
        """Arm the heartbeat at the current clock time and drive it here.

        Called once from the run's own event loop, immediately after the first
        process starts, so the catalogue checks and before-hooks that precede a
        spawn are never counted as idle time.
        """
        self.schedule = _IdleSchedule.start(self.interval, self.clock())
        self.task = asyncio.create_task(self.run())

    def note_activity(self) -> None:
        """Record a non-empty read on any monitored stream."""
        schedule = self.schedule
        if schedule is not None:
            schedule.note_activity(self.clock())

    def poll(self) -> None:
        """Emit one notification when one is due, rescheduling either way."""
        schedule = self.schedule
        if schedule is None:
            return
        due = schedule.take_due(self.clock())
        if due is None:
            return
        elapsed_total, elapsed_idle = due
        if not self.notify(elapsed_total, elapsed_idle):
            schedule.stop()

    async def run(self) -> None:
        """Poll the schedule until it is stopped or this task is cancelled."""
        schedule = self.schedule
        if schedule is None:
            return
        while not schedule.is_stopped:
            delay = schedule.seconds_until_due(self.clock())
            if delay > 0:
                await asyncio.sleep(delay)
            self.poll()

    def stop(self) -> None:
        """Stop the schedule; idempotent, and safe before :meth:`launch`."""
        schedule = self.schedule
        if schedule is not None:
            schedule.stop()


async def _stop_idle_monitor(monitor: _IdleMonitor | None) -> None:
    """Stop *monitor* and settle its driver exactly once; safe to repeat.

    Repeats are how a run's several exit paths share one watchdog without
    coordinating: whichever path runs first cancels the driver and clears the
    handle, and every later call is a no-op.
    """
    if monitor is None:
        return
    monitor.stop()
    task = monitor.task
    monitor.task = None
    if task is None:
        return
    task.cancel()
    (outcome,) = await asyncio.gather(task, return_exceptions=True)
    _report_driver_failure(outcome)


def _build_idle_monitor(
    interval: float | None,
    on_idle: cabc.Callable[[float, float], None] | None,
    subject: str,
    sink: typ.IO[str] | None = None,
) -> _IdleMonitor | None:
    """Build a run-owned heartbeat, or ``None`` when idle reporting is off.

    The returned monitor is not yet armed: the caller launches it once the
    run's first process has started, so the clock's zero point is the spawn
    rather than the option parsing that preceded it.

    Returns
    -------
    _IdleMonitor | None
        A monitor for *interval* seconds, or ``None`` when idle reporting is
        disabled.
    """
    if interval is None:
        return None
    mirror = _MirrorCursor()
    return _IdleMonitor(
        interval=interval,
        notify=_IdleNotifier(
            callback=on_idle,
            diagnostic=_IdleDiagnostic(
                subject=subject,
                destination=_LiveSink(sink),
                # Read at emission time, so the diagnostic sees where the echo
                # sink is *now* rather than where it was when the run started.
                is_line_open=lambda: mirror.is_mid_line,
            ),
        ),
        mirror=mirror,
    )


def _validate_idle_options(
    interval: float | None,
    on_idle: cabc.Callable[[float, float], None] | None,
) -> float | None:
    """Validate one output options object's idle contract, normalizing it.

    The normalized interval is returned rather than merely checked, because it
    is what the schedule later does arithmetic on: ``_IdleSchedule.start``
    evaluates ``now + interval``, so a value that converts to a float but is
    not one — the string ``"30"``, say — would raise ``TypeError`` from inside
    the run's driver, after the child had already been spawned. Validation is
    the last point at which that is still the caller's error to see.

    Parameters
    ----------
    interval : float | None
        The requested idle interval in seconds; ``None`` disables idle
        reporting entirely.
    on_idle : collections.abc.Callable[[float, float], None] | None
        The caller's synchronous notification callback, if any.

    Returns
    -------
    float | None
        *interval* as a finite float, or ``None`` when reporting is disabled.

    Raises
    ------
    TypeError
        If *on_idle* is not callable, is detectably asynchronous, or *interval*
        is not a real number of seconds.
    ValueError
        If *interval* is not finite and strictly positive, or *on_idle* was
        supplied without an interval to schedule it on.
    """
    if on_idle is not None and not callable(on_idle):
        msg = f"on_idle must be callable, got {type(on_idle).__name__}"
        raise TypeError(msg)
    if interval is None:
        if on_idle is not None:
            msg = "on_idle requires idle_after to be set"
            raise ValueError(msg)
        return None
    if _is_async_callback(on_idle):
        msg = "on_idle must be a synchronous callback"
        raise TypeError(msg)
    seconds = _resolve_idle_interval(interval)
    if seconds <= 0:
        msg = f"idle_after must be strictly positive, got {interval!r}"
        raise ValueError(msg)
    return seconds


def _resolve_idle_interval(interval: float) -> float:
    """Coerce an idle interval to a finite float.

    Returns
    -------
    float
        *interval* as a finite number of seconds.

    Raises
    ------
    TypeError
        If *interval* is not convertible to a float, or overflows one.
    ValueError
        If *interval* converts to a non-finite value such as ``nan`` or
        ``inf``.
    """
    try:
        seconds = float(interval)
    except (OverflowError, TypeError, ValueError) as exc:
        msg = f"idle_after must be a finite number of seconds, got {interval!r}"
        raise TypeError(msg) from exc
    if not math.isfinite(seconds):
        msg = f"idle_after must be finite, got {interval!r}"
        raise ValueError(msg)
    return seconds


def _is_async_callback(callback: object) -> bool:
    """Return whether a callback is detectably asynchronous."""
    if inspect.iscoroutinefunction(callback):
        return True
    return inspect.iscoroutinefunction(type(callback).__call__)


def _report_driver_failure(outcome: object) -> None:
    """Report a driver task that ended for a reason other than a stop."""
    match outcome:
        case BaseException() if not isinstance(outcome, asyncio.CancelledError):
            _LOGGER.warning(
                "idle_heartbeat_failed error=%s",
                type(outcome).__name__,
                extra={
                    "cuprum_action": "idle_heartbeat_failed",
                    "cuprum_error_type": type(outcome).__name__,
                },
            )
        case _:
            # The gather result of a driver that simply ended, or the
            # ``CancelledError`` every ordinary stop produces: not a failure.
            return


__all__ = [
    "_IdleMonitor",
    "_IdleNotifier",
    "_IdleSchedule",
    "_build_idle_monitor",
    "_stop_idle_monitor",
    "_validate_idle_options",
]
