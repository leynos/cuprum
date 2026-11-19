"""Locate the asyncio transport behind a stream object.

Several callers need the transport a stream is attached to — extracting its
descriptor for the native hand-off, closing it when a post-close drain times
out, or closing it while a startup read is abandoned. The lookup is the same
in each case, so it lives here rather than inside any one caller's module.
This module is a leaf: it imports nothing from Cuprum and holds no state.

Reuse policy: a caller that needs a stream's transport imports it from here.
Do not grow this module into a stream lifecycle helper; the descriptor
extraction and pause lifecycle belong to
:mod:`cuprum._pipeline_stream_fds`, and the pump's own teardown belongs to
:mod:`cuprum._streams_pump`.
"""

from __future__ import annotations


def _stream_transport(stream: object) -> object | None:
    """Return a stream's transport, tolerating the private attribute name.

    ``asyncio.StreamWriter`` publishes ``transport``; ``StreamReader`` keeps it
    on ``_transport``, so both spellings are consulted.

    Returns
    -------
    object or None
        The stream's transport, or ``None`` when it exposes neither attribute.
    """
    transport = getattr(stream, "transport", None)
    if transport is None:
        transport = getattr(stream, "_transport", None)
    return transport


__all__ = ["_stream_transport"]
