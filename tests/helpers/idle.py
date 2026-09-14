"""Shared helpers for the idle-heartbeat execution tests.

The heartbeat's timing is pinned without a clock in
``cuprum/unittests/test_idle_heartbeat.py``. The tests that use these helpers
run real children instead, and so observe *ordering* only -- whether a
deadline moved, whether a notification followed a reset -- because how a real
child is scheduled is not something a test may assert about directly.
"""

from __future__ import annotations

import asyncio
import itertools
import typing as typ

if typ.TYPE_CHECKING:
    import io

KEEPALIVE_PREFIX = "[cuprum]"


def pending_tasks() -> list[asyncio.Task[object]]:
    """Return the running loop's unfinished tasks, excluding the caller's own.

    Returns
    -------
    list[asyncio.Task[object]]
        Every task the loop still has pending, so a run that left a watchdog or
        a stream consumer behind shows up here rather than at interpreter exit.
    """
    current = asyncio.current_task()
    return [
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    ]


def keepalives(sink: io.StringIO) -> list[str]:
    """Return the keepalive lines written to *sink*.

    Returns
    -------
    list[str]
        Every line of the sink that the built-in renderer produced, in order.
    """
    return [
        line
        for line in sink.getvalue().splitlines()
        if line.startswith(KEEPALIVE_PREFIX)
    ]


class IdleRecorder:
    """Record every idle notification one run produced, in order."""

    def __init__(self) -> None:
        """Start with no notifications."""
        self.seen: list[tuple[float, float]] = []

    def __call__(self, elapsed_total: float, elapsed_idle: float) -> None:
        """Record one ``(elapsed_total, elapsed_idle)`` notification."""
        self.seen.append((elapsed_total, elapsed_idle))

    def idles(self) -> list[float]:
        """Return the reported idle ages, in order."""
        return [idle for _total, idle in self.seen]

    def resets(self) -> int:
        """Return how often the reported idle age fell back.

        Returns
        -------
        int
            The number of notifications that reported *less* idle time than the
            one before them. Output is the only thing that moves the idle clock
            backwards, so this counts how many quiet spells a child's output
            ended -- with no clock of its own, and no wall-clock assertion.
        """
        return sum(
            1 for earlier, later in itertools.pairwise(self.idles()) if later < earlier
        )

    def reset_after_output(self) -> bool:
        """Report whether a later idle age is smaller than an earlier one.

        Returns
        -------
        bool
            ``True`` when some notification reports less idle time than one
            before it, which is what a child's resumed output produces.
        """
        return self.resets() > 0

    def first_total(self) -> float:
        """Return the total runtime the first notification reported."""
        return self.seen[0][0]
