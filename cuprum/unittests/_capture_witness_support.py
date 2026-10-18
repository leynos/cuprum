"""Witnesses that a capture test's spill really happened.

A cleanup test asserts an empty spill directory, and a directory is equally
empty for a run that never spilled. The witness closes that gap: it records the
events the run published and refuses to let the removal assertion stand unless
a ``SPILLED_TO_FILE`` event was among them, so "nothing was left behind" cannot
be satisfied by "nothing was written".

Both the pipeline and the line-stream cleanup suites need the same witness, and
a second copy would let one drift from the other — the assertion text is part of
what a failing run reports.
"""

from __future__ import annotations

from cuprum.capture_events import CaptureEvent, CaptureEventCategory


class _SpillWitness:
    """Record every capture event the run emits."""

    def __init__(self) -> None:
        """Start with no events seen."""
        self.categories: list[CaptureEventCategory] = []

    def __call__(self, event: CaptureEvent) -> None:
        """Record one capture event's category."""
        self.categories.append(event.category)

    def spilled(self) -> None:
        """Assert at least one stream really spilled."""
        assert CaptureEventCategory.SPILLED_TO_FILE in self.categories, (
            "the run never spilled, so the removal assertion would hold for "
            f"the wrong reason; events seen: {self.categories}"
        )
