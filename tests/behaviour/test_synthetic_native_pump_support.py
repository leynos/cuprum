"""Regression coverage for the scoped synthetic native-pump test path."""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum import _pipeline_streams
from tests.behaviour._synthetic_native_pump_support import (
    force_synthetic_native_pump_path,
)

if typ.TYPE_CHECKING:
    import pytest


class _ClosedWriteTransport:
    """Minimal transport accepted by a test-only ``StreamWriter``."""

    def is_closing(self) -> bool:
        """Prevent the writer finalizer from trying to close an owned transport."""
        return True


def test_synthetic_path_reaches_submission_and_restores_windows_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic pipes bypass only the scoped Windows platform guard."""
    submissions: list[_pipeline_streams._RustPumpHandoff] = []

    async def record_submission(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
        *,
        handoff: _pipeline_streams._RustPumpHandoff,
    ) -> bool:
        """Record native submission without performing synchronous I/O."""
        del reader, writer
        submissions.append(handoff)
        await asyncio.sleep(0)
        return True

    async def dispatch_synthetic_streams() -> None:
        """Construct enough stream identity for the test-only FD extractor."""
        reader = asyncio.StreamReader()
        writer = asyncio.StreamWriter(
            typ.cast("asyncio.WriteTransport", _ClosedWriteTransport()),
            typ.cast("asyncio.StreamReaderProtocol", object()),
            reader,
            asyncio.get_running_loop(),
        )
        await _pipeline_streams._pump_stream_dispatch(reader, writer)

    monkeypatch.setattr(
        _pipeline_streams,
        "_native_pump_supported_on_platform",
        lambda: False,
    )
    monkeypatch.setattr(_pipeline_streams, "_run_rust_pump", record_submission)
    assert _pipeline_streams._native_pump_supported_on_platform() is False, (
        "the simulated Windows predicate must decline real native pumping"
    )

    with force_synthetic_native_pump_path(monkeypatch):
        assert _pipeline_streams._native_pump_supported_on_platform() is True, (
            "only the scoped synthetic path may bypass the Windows guard"
        )
        asyncio.run(dispatch_synthetic_streams())

    assert len(submissions) == 1, "synthetic descriptors must reach submission"
    assert _pipeline_streams._native_pump_supported_on_platform() is False, (
        "the production Windows predicate must be restored after the context"
    )
