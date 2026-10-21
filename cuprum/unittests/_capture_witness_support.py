"""Witnesses that a capture test's spill really happened.

A cleanup test asserts an empty spill directory, and a directory is equally
empty for a run that never spilled. The witness closes that gap: it records the
events the run published and refuses to let the removal assertion stand unless
a ``SPILLED_TO_FILE`` event was among them, so "nothing was left behind" cannot
be satisfied by "nothing was written".

The pipeline and line-stream suites need only the event record. The spill
cleanup suite needs more: on the routes that return no record naming a file it
asserts against the directory itself, so the witness snapshots the names
present at each spill transition when it is given a directory. Both roles live
here because a second copy of either would let one drift from the other — the
assertion text is part of what a failing run reports.
"""

from __future__ import annotations

import typing as typ

from cuprum.capture_events import CaptureEvent, CaptureEventCategory

if typ.TYPE_CHECKING:
    import pathlib


class _SpillWitness:
    """Record every capture event the run emits, and each spill's directory."""

    def __init__(self, directory: pathlib.Path | None = None) -> None:
        """Start with no events seen, watching *directory* when one is given."""
        self.directory = directory
        # Entry *names* rather than paths, so a case can assert a file appeared
        # without pinning the temporary name.
        self.snapshots: list[tuple[str, ...]] = []
        self.categories: list[CaptureEventCategory] = []

    def __call__(self, event: CaptureEvent) -> None:
        """Record one event's category, and a spill's directory listing."""
        self.categories.append(event.category)
        directory = self.directory
        if (
            directory is not None
            and event.category is CaptureEventCategory.SPILLED_TO_FILE
        ):
            self.snapshots.append(tuple(entry.name for entry in directory.iterdir()))

    def spilled(self) -> None:
        """Assert at least one stream really spilled."""
        assert CaptureEventCategory.SPILLED_TO_FILE in self.categories, (
            "the run never spilled, so the removal assertion would hold for "
            f"the wrong reason; events seen: {self.categories}"
        )

    def awaited(self) -> tuple[str, ...]:
        """Return the first snapshot, asserting a spill was actually seen.

        Returns
        -------
        tuple[str, ...]
            The directory entry names recorded at the first spill.
        """
        assert self.snapshots, (
            "the run never spilled, so this case would pass because no file "
            f"was ever made; events seen: {self.categories}"
        )
        return self.snapshots[0]
