"""Compatibility boundary for subprocess stream-drain helpers.

The live lifecycle implementation resides in :mod:`cuprum._subprocess_wait`,
which also owns the timeout and teardown observability context. This module
keeps focused drain tests and imports on a narrow, explicit boundary.
"""

from cuprum._subprocess_wait import (
    _CAPTURE_EOF_GRACE_S,
    _cancel_pending_consumers,
    _decode_consumer_result,
    _drain_stream_consumers,
    _settle_consumers,
)

__all__ = [
    "_CAPTURE_EOF_GRACE_S",
    "_cancel_pending_consumers",
    "_decode_consumer_result",
    "_drain_stream_consumers",
    "_settle_consumers",
]
