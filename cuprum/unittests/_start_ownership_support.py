"""Shared machinery for the spawned-child startup-abandonment tests.

Two paths spawn a child and then run synchronous observe hooks inline: the
single-command run, in either its direct or its streamed shape, and every
pipeline stage. Both owe the same guarantee when a ``start`` hook raises, so
both are specified with the same failing hook, the same blocking child, and the
same reading of the recorded pid. Keeping those here stops the two
specifications drifting apart.

The child blocks without limit: it exists to be terminated, never to finish.
Nothing here waits on the wall clock, because a child that was never reaped is
a failure whether or not the test would have noticed in time.
"""

from __future__ import annotations

import sys
import typing as typ

from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.program import Program
from cuprum.unittests._wait4_process_support import _ChildOwnership, _probe_child
from tests.helpers.timeouts import python_interpreter

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent

__all__ = [
    "_BLOCKING_CHILD",
    "_StartHookError",
    "_assert_strictly_reaped",
    "_failing_start_hook",
    "_python_catalogue",
    "_recording_hook",
]

# Long enough that the child cannot exit on its own, so a run that ends can
# only have ended because the parent terminated it.
_BLOCKING_CHILD = "import time; time.sleep(300)"

#: Platforms whose own ``wait4`` reaper leaves a child this process can tell
#: apart from one the test's probe had to collect. Everywhere else the child
#: is still asserted dead, but not with that distinction.
_STRICT_OWNERSHIP = sys.platform.startswith("linux") or sys.platform == "darwin"


class _StartHookError(Exception):
    """Raised by a deliberately failing observe hook in these tests."""


def _python_catalogue(name: str) -> tuple[Program, ProgramCatalogue]:
    """Build a Python program binding and the catalogue that admits it."""
    program = Program(python_interpreter())
    project = ProjectSettings(
        name=name,
        programs=(program,),
        documentation_locations=("docs/users-guide.md",),
        noise_rules=(),
    )
    return program, ProgramCatalogue(projects=(project,))


def _failing_start_hook(events: list[ExecEvent]) -> cabc.Callable[[ExecEvent], None]:
    """Return a synchronous hook that records events and fails on ``start``."""

    def hook(event: ExecEvent) -> None:
        """Record every event, and fail once the child has started."""
        events.append(event)
        if event.phase == "start":
            raise _StartHookError

    return hook


def _recording_hook(events: list[ExecEvent]) -> cabc.Callable[[ExecEvent], None]:
    """Return a synchronous hook that records every event without raising."""

    def hook(event: ExecEvent) -> None:
        """Record one lifecycle event."""
        events.append(event)

    return hook


def _assert_strictly_reaped(pid: int) -> None:
    """Assert the run's own reaper collected the child, where a host can tell.

    ``REAPED`` is the failure this exists to catch: the probe collected an
    exited-but-unclaimed child, which means the run terminated it without
    waiting for it and left the status for whoever came next.
    """
    if not _STRICT_OWNERSHIP:
        return
    ownership = _probe_child(pid)
    assert ownership is _ChildOwnership.COLLECTED, (
        f"child {pid} must be collected by the run, not left {ownership.name}"
    )
