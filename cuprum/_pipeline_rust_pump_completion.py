"""Resources retained until a Rust-pump executor future settles."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum.pump_span_observation import _PumpHopSpans


@dc.dataclass(frozen=True, slots=True)
class _RustPumpCompletion:
    """Resources whose lifetime ends when the native worker settles."""

    cleanup_complete: asyncio.Future[None]
    pump_hop_spans: _PumpHopSpans
    rust_writer_fd: int
    state: object
