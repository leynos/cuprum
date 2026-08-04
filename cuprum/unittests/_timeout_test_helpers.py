"""Shared test doubles for the subprocess timeout test modules.

The timeout suites are split by concern — cleanup semantics, structured
logging, and observe events — but all three drive the same wait/teardown code
and therefore need the same process, observation, and execution stand-ins.
They live here so the split modules share one definition rather than drifting
apart.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _EventDetails
    from cuprum.sh import ExecutionContext


class _TimeoutWaitProcess:
    """Process double whose ``wait()`` blocks until terminate()/kill()."""

    def __init__(self) -> None:
        """Start unexited, with a pid and unset started/termination events."""
        self.returncode: int | None = None
        self.pid = 4321
        self._exited = asyncio.Event()
        self.wait_started = asyncio.Event()

    async def wait(self) -> int:
        """Block until a termination signal records an exit code."""
        self.wait_started.set()
        await self._exited.wait()
        assert self.returncode is not None, (
            "process double invariant: terminate()/kill() must record "
            "returncode before _exited is set"
        )
        return self.returncode

    def terminate(self) -> None:
        """Record a SIGTERM exit code and release ``wait()``."""
        if self.returncode is None:
            self.returncode = -15
        self._exited.set()

    def kill(self) -> None:
        """Record a SIGKILL exit code and release ``wait()``."""
        if self.returncode is None:
            self.returncode = -9
        self._exited.set()


class _ExitedProcess:
    """Process double whose ``wait()`` returns immediately (already exited)."""

    def __init__(self, returncode: int = 0) -> None:
        """Start already exited, with a recorded exit code and a pid."""
        self.returncode = returncode
        self.pid = 5678

    async def wait(self) -> int:
        """Return the recorded exit code without ever suspending."""
        return self.returncode

    def terminate(self) -> None:
        """No-op: the process has already exited."""

    def kill(self) -> None:
        """No-op: the process has already exited."""


class _Observation(typ.Protocol):
    """Structural contract these doubles satisfy: an ``emit`` and nothing else.

    The production ``_StageObservation.emit`` narrows ``phase`` to a
    ``Literal``; the doubles accept any ``str``, which is the wider and
    therefore structurally compatible shape. Typing against this protocol lets
    a helper accept either double without casting one to the other — a cast
    that would be a lie, since the objects are unrelated.
    """

    def emit(self, phase: str, details: _EventDetails) -> None:
        """Handle an emitted observe event."""


@dc.dataclass(slots=True)
class _RecordingObservation:
    """Observation double recording emitted ``(phase, _EventDetails)`` pairs."""

    events: list[tuple[str, _EventDetails]] = dc.field(default_factory=list)

    def emit(self, phase: str, details: _EventDetails) -> None:
        """Record an emitted observe event."""
        self.events.append((phase, details))


@dc.dataclass(slots=True)
class _RaisingObservation:
    """Observation double whose ``emit`` always raises, simulating a bad hook."""

    def emit(self, phase: str, details: _EventDetails) -> typ.NoReturn:
        """Raise as if an observe hook failed while handling the event."""
        _ = (phase, details)
        msg = "observe hook exploded"
        raise RuntimeError(msg)


@dc.dataclass(slots=True)
class _DeadlineExecution:
    """Execution stand-in exposing only the fields the waiter reads."""

    ctx: ExecutionContext
    timeout: float | None
    observation: _Observation = dc.field(default_factory=_RecordingObservation)


__all__ = [
    "_DeadlineExecution",
    "_ExitedProcess",
    "_RaisingObservation",
    "_RecordingObservation",
    "_TimeoutWaitProcess",
]
