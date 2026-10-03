"""Repeated cancellation of a run, and the rescue that bounds it.

Kept apart from the scenario scaffolding that uses it because this is the part
with a failure mode of its own: a run's teardown is deliberately uninterruptible
(``_shielded_cleanup`` absorbs every cancellation until its cleanup finishes),
so a teardown that wedges cannot be moved from inside the loop. The watchdog
thread is what covers that, and the flag it sets is reported rather than
swallowed — the scenario refuses an outcome that needed rescuing, because the
descendant's death would then be the harness's work rather than the run's.

What these scenarios cancel *twice* is the interesting case: the first
cancellation is consumed by the teardown the run is already performing, so the
second is what would otherwise skip the ``SIGKILL`` escalation and leave a
``SIGTERM``-immune descendant running.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import os
import signal
import threading
import time

# A repeated cancellation has to land *inside* the teardown the first one
# started. The run's grace period is what keeps it in flight that long, so this
# interval samples it rather than racing the whole teardown.
_CANCEL_INTERVAL_S = 0.05


@dc.dataclass(frozen=True, slots=True)
class _Cancellation:
    """How a run responded to being cancelled.

    Attributes
    ----------
    landings:
        How many cancellations found the run still running.
    raised:
        The exception the run ended on, or ``"returned"``.
    elapsed:
        Wall-clock seconds from the first cancellation to the run settling.
    rescued:
        Whether teardown had to be unblocked.
    """

    landings: int
    raised: str
    elapsed: float
    rescued: bool


class _RescueWatchdog:
    """Force a stuck run to settle, in a thread of its own.

    A run's teardown is cancellation-safe by design: ``_shielded_cleanup``
    absorbs every cancellation until its cleanup finishes, so an *in*-loop
    timeout cannot interrupt a teardown that is stuck. Cancelling harder would
    only repeat a request the loop has already declined to honour.

    Killing the descendant the teardown is waiting on is therefore the one
    thing that can unblock it, and a thread is what can do that while the loop
    is busy. That a rescue was needed is recorded rather than hidden; the
    scenario's assertions refuse the outcome on that basis.
    """

    def __init__(self, pid: int, grace_seconds: float) -> None:
        """Start watching *pid*, arming a kill for after the grace period."""
        self._pid = pid
        self._timer = threading.Timer(grace_seconds, self._rescue)
        self.rescued = False

    def _rescue(self) -> None:
        """End the process the teardown is waiting on, recording that it stuck."""
        self.rescued = True
        # Never a group: the descendant's own group is what the stuck teardown
        # failed to signal, and the run's group is the only one in scope.
        with contextlib.suppress(ProcessLookupError):
            os.kill(self._pid, signal.SIGKILL)

    def __enter__(self) -> _RescueWatchdog:
        """Arm the watchdog."""
        self._timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Disarm the watchdog, so a settled run arms nothing."""
        self._timer.cancel()


async def _cancel_run(
    task: asyncio.Task[object],
    grandchild: int,
    cancellations: int,
    settle_bound: float,
) -> _Cancellation:
    """Cancel the run *cancellations* times and report how it responded."""
    landed = 0
    cancelled_at = time.monotonic()
    with _RescueWatchdog(grandchild, settle_bound) as watchdog:
        for _ in range(cancellations):
            # A cancellation arriving after the run has settled proves nothing
            # about interrupted cleanup, so it is not counted as one that did.
            if task.done():
                break
            task.cancel()
            landed += 1
            # Yield between cancellations so a repeat arrives while the
            # shielded teardown is still working, rather than coalescing onto
            # the request the first one already made.
            await asyncio.sleep(_CANCEL_INTERVAL_S)
        try:
            # Bounded for the benefit of the cancellation itself, not as a
            # guard on a stuck teardown: the shield absorbs this timeout the
            # same way it absorbs any other cancellation. The watchdog is what
            # covers that case.
            async with asyncio.timeout(settle_bound):
                await task
        except asyncio.CancelledError:
            raised = "CancelledError"
        except TimeoutError:
            raised = "TimeoutError"
        except BaseException as exc:  # ruff: ignore[blind-except] - report what it saw
            raised = type(exc).__name__
        else:
            raised = "returned"
    return _Cancellation(
        landings=landed,
        raised=raised,
        elapsed=time.monotonic() - cancelled_at,
        rescued=watchdog.rescued,
    )
