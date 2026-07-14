"""Shared fixtures for Command-Query Separation tests."""

from __future__ import annotations

from cuprum import ECHO, sh
from cuprum.events import ExecEvent


def _echo_cmd() -> sh.SafeCmd:
    """Return a trivial ``SafeCmd`` for the allowlisted ``echo`` program."""
    return sh.make(ECHO)("hello")


def _event() -> ExecEvent:
    """Return a minimal ``plan`` event for hook dispatch."""
    return ExecEvent(
        phase="plan",
        program=ECHO,
        argv=(str(ECHO),),
        cwd=None,
        env=None,
        pid=None,
        timestamp=0.0,
        line=None,
        exit_code=None,
        duration_s=None,
        tags={},
    )
