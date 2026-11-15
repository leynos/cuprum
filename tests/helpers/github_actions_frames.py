"""Shared byte-level helpers for buffered GitHub Actions framing tests.

Child echo writes raw bytes through ``log.buffer`` and flushes that buffer
itself, so tests that pin workflow-command order inspect the underlying byte
buffer rather than the text layer they would see through a read of the
destination. Keeping these helpers in one place stops the exact frame shape
from drifting between the unit and end-to-end suites.
"""

from __future__ import annotations

import io

_LEASE_PREFIX = b"::stop-commands::"
_ENDGROUP = b"::endgroup::\n"


def buffered_log() -> tuple[io.TextIOWrapper, io.BytesIO]:
    """Build a non-line-buffered text wrapper over an inspectable byte buffer."""
    raw = io.BytesIO()
    log = io.TextIOWrapper(
        raw,
        encoding="utf-8",
        newline="",
        line_buffering=False,
        write_through=False,
    )
    return log, raw


def stop_token(value: bytes) -> bytes:
    """Return the lease token from a framed run's byte output."""
    return next(
        line.removeprefix(_LEASE_PREFIX)
        for line in value.split(b"\n")
        if line.startswith(_LEASE_PREFIX)
    )


def expected_frame(title: str, token: bytes, payload: bytes) -> bytes:
    """Build the exact bytes of one frame with a single payload line."""
    return (
        f"::group::{title}\n".encode()
        + _LEASE_PREFIX
        + token
        + b"\n"
        + payload
        + b"::"
        + token
        + b"::\n"
        + _ENDGROUP
    )
