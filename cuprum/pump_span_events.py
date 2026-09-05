"""Stable names and outcomes for opt-in Rust-pump hop spans.

The executor-hop spans deliberately expose only bounded transfer facts. They
are independent of :mod:`cuprum.pump_events`, whose routing and cleanup events
remain a separate metrics-oriented observation channel.
"""

from __future__ import annotations

import typing as typ

from cuprum.adapters._support import _prefixed

type PumpHopOutcome = typ.Literal[
    "succeeded",
    "failed",
    "cancelled",
    "failed_after_cancel",
]
"""The bounded terminal outcomes of one Rust-pump executor hop."""

PUMP_HOP_SPAN_NAME = "cuprum.rust_pump_hop"
"""The tracing span name used for one Rust-pump executor hop."""

_attribute = _prefixed("cuprum.")

PUMP_HOP_OUTCOME_ATTRIBUTE = _attribute("outcome")
PUMP_HOP_OPERATION_ATTRIBUTE = _attribute("operation")
PUMP_HOP_BUFFER_SIZE_ATTRIBUTE = _attribute("buffer_size")
PUMP_HOP_TOTAL_BYTES_ATTRIBUTE = _attribute("total_bytes")

__all__ = [
    "PUMP_HOP_BUFFER_SIZE_ATTRIBUTE",
    "PUMP_HOP_OPERATION_ATTRIBUTE",
    "PUMP_HOP_OUTCOME_ATTRIBUTE",
    "PUMP_HOP_SPAN_NAME",
    "PUMP_HOP_TOTAL_BYTES_ATTRIBUTE",
    "PumpHopOutcome",
]
