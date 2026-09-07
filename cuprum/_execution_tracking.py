"""The hook set and background tasks one command execution carries.

A leaf module so both the ``cuprum.sh`` facade and the line-iteration driver it
delegates to can name the bundle without either importing the other.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._pipeline_internals import _ExecutionHooks
    from cuprum.sinks import OutputSession


@dc.dataclass(frozen=True, slots=True)
class _ExecutionTracking:
    """Hook and task tracking for command execution."""

    execution_hooks: _ExecutionHooks
    pending_tasks: list[asyncio.Task[None]]
    sink_session: OutputSession | None = None


__all__ = ["_ExecutionTracking"]
