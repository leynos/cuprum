"""Per-drain echo bookkeeping shared by the stream consumers.

A drain of one subprocess stream owns three small pieces of mutable state:
the collector for handled echo-disablement records that becomes
``CommandResult.relay_fallbacks``, the guard that stops further echo writes
after the first handled failure, and the cursor recording whether a mirrored
sink is mid-line for the idle heartbeat. ``cuprum._streams`` re-exports these
names, so importers of that module keep working unchanged.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.echo_events import RelayFallback


@dc.dataclass(slots=True)
class _RelayDiagnostics:
    """Per-drain collector for handled echo-disablement records.

    One collector belongs to one command stream. Because the echo guard stops
    any later echo write after the first handled failure, a drain appends at
    most one :class:`~cuprum.echo_events.RelayFallback` here.
    """

    fallbacks: list[RelayFallback] = dc.field(default_factory=list)
    is_settled: bool = False

    def settle(self) -> None:
        """Publish the collected records for the owning command's result.

        Idempotent: the reconciliation paths run exactly once per drain, and a
        second call keeps whichever record list that call captured.
        """
        self.is_settled = True

    def snapshot(self) -> tuple[RelayFallback, ...]:
        """Return the collected records, or ``()`` before the drain settled.

        A drain that never settled — cancelled or abandoned during teardown —
        leaves its records unread: those diagnostics remain on the echo
        observation channel, so callers on a non-result path see ``()``.

        Returns
        -------
        tuple[RelayFallback, ...]
            The records collected before settlement, empty when the drain
            never settled or recorded nothing.
        """
        if not self.is_settled:
            return ()
        return tuple(self.fallbacks)


@dc.dataclass(slots=True)
class _EchoGuard:
    """Mutable holder tracking whether echo is disabled for one drain."""

    disabled: bool = False


@dc.dataclass(slots=True)
class _MirrorCursor:
    """Presentation-only record of whether a mirrored sink is mid-line.

    Shared with the idle heartbeat, which needs to know whether the last bytes
    echoed to the parent's stderr ended a line: a keepalive written now would
    otherwise become the tail of an unfinished mirrored line. Recording the
    position here, on the echo path, keeps the diagnostic free of any
    knowledge about the child's stream, and nothing in this class can affect
    what was captured.
    """

    is_mid_line: bool = False

    def note(self, chunk: bytes) -> None:
        """Record one written echo chunk; an empty chunk changes nothing."""
        if chunk:
            self.is_mid_line = not chunk.endswith(b"\n")


__all__ = ["_EchoGuard", "_MirrorCursor", "_RelayDiagnostics"]
