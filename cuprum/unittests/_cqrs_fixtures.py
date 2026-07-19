"""Shared CQRS fixtures for `cuprum.unittests`.

This module keeps the reusable fixtures for Command-Query Separation tests in
one place so related cases do not repeat the same `cuprum.sh` and
`cuprum.events.ExecEvent` setup. `_echo_cmd()` supplies the allowlisted
`echo` command for command-building assertions, and `_event()` supplies the
matching `ExecEvent` for hook-dispatch assertions.

Examples
--------
- `_echo_cmd()` creates `sh.make(ECHO)("hello")` for command-path tests.
- `_event()` creates a plan-phase `ExecEvent` with the `echo` program.
"""

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
