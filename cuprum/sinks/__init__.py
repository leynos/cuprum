"""Presentation sink adapters for parent-facing run output.

A sink adapter reshapes how a run's parent-facing output appears — for
example, framing it in a GitHub Actions group — without changing capture,
success semantics, or the returned result. Sinks are opt-in: a caller passes
one explicitly via :class:`cuprum.RunOutputOptions.sink`, and runs without a
sink are byte-for-byte unchanged.

The protocol lives in :mod:`cuprum.sinks.base`; concrete adapters (such as
:class:`cuprum.sinks.GitHubActionsSink`) live alongside it in this package.
"""

from __future__ import annotations

from cuprum.sinks.base import (
    OutputSession,
    OutputSink,
    SessionOutcome,
    SessionStart,
    TerminalOutcome,
)
from cuprum.sinks.github_actions import (
    GitHubActionsSession,
    GitHubActionsSink,
)

__all__ = [
    "GitHubActionsSession",
    "GitHubActionsSink",
    "OutputSession",
    "OutputSink",
    "SessionOutcome",
    "SessionStart",
    "TerminalOutcome",
]
