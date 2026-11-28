"""The records a run hands to its stream-consumer spawn.

Split from ``cuprum._subprocess_streams`` when the live byte tap pushed that
module past the repository's 400-line ceiling. The seam is the one the code
already drew: these are the values a run has resolved *before* any task
exists — the two stream configurations, the pid, the diagnostics collectors,
the discard latch, the idle monitor, and now the live tap — while the module
that consumes them owns the task construction and the wiring rules.

Keeping the records here means the spawn module and the handle's attachment
path name the same types without either owning the other's behaviour, and a
test that replaces a spawn seam still patches the module that calls it.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._capture_types import CaptureSink
    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._stream_live import _LiveStreamSinks
    from cuprum._streams import _RelayDiagnostics, _StreamConfig

__all__ = [
    "_StreamConsumerSpawnContext",
    "_StreamSpawnInputs",
    "_StreamSpawnOptions",
]


@dc.dataclass(frozen=True, slots=True)
class _StreamSpawnOptions:
    """The optional latches and live tap a spawn may reuse.

    Bundled rather than passed as three keywords so the spawn helper's
    signature stays inside the repository's argument limit. Each field is
    optional: the direct run supplies neither latch, and a text run supplies
    no live tap.
    """

    discard_on_cancel: asyncio.Event | None = None
    settlement_started: asyncio.Event | None = None
    live: _LiveStreamSinks | None = None


_NO_SPAWN_OPTIONS = _StreamSpawnOptions()


@dc.dataclass(frozen=True, slots=True)
class _StreamConsumerSpawnContext:
    """Inputs a run hands to its stream-consumer spawn.

    Bundles the *resolved* per-stream configurations, the subprocess PID, and
    the per-stream relay diagnostics collectors so the spawn helper takes one
    argument instead of three. The context is created before any consumer task
    exists; ownership of the collectors stays with the run, which continues to
    retain the same tuple on its ``_RunTaskOwnership``.

    The configurations are resolved here rather than inside the spawn so that
    the run holds the very objects its consumers drain. That is what makes the
    capture accumulators reachable from teardown: a config carries the
    accumulator for its own policy, and a run that rebuilt the configs would
    hold accumulators nobody drained into while the ones holding a spill file
    went out of reach.
    """

    stdout_config: _StreamConfig
    stderr_config: _StreamConfig
    pid: int | None
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics]

    @property
    def captures(self) -> tuple[CaptureSink | None, CaptureSink | None]:
        """Report both streams' accumulators, stdout first.

        The order matches ``relay_diagnostics`` and the consumer pair, so a
        teardown indexed by stream reaches the right accumulator for whichever
        stream it is cleaning up.
        """
        return (self.stdout_config.capture_sink, self.stderr_config.capture_sink)


@dc.dataclass(frozen=True, slots=True)
class _StreamSpawnInputs:
    """Everything a streamed run needs before it starts its consumers.

    The three runs that own a child's streams — the direct run, the line
    iterator, and the owned handle — build the same configuration, the same
    diagnostics tuple, and the same spawn context. Bundling them here keeps
    that construction in one place, because each caller documents the same
    invariant: the consumers it starts must be wired exactly as the others
    wire theirs, or the paths silently diverge on capture, echo, and sink
    selection.

    ``discard_on_cancel``, ``relay_diagnostics``, and ``idle`` are carried
    alongside the context because the run retains all three on its
    ``_RunTaskOwnership``: the context is a value snapshot for the spawn, not a
    hand-off of ownership.

    ``live`` is the byte tap a byte run attaches to both consumers. It travels
    with the resolved configurations because the tap and the capped read size
    must agree, and it is the only field a text run leaves at its default.
    """

    discard_on_cancel: asyncio.Event
    spawn_context: _StreamConsumerSpawnContext
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics]
    idle: _IdleMonitor | None
    live: _LiveStreamSinks | None = None
