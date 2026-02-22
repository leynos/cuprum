"""Test-only re-exports of internal helpers.

Cuprum keeps most implementation details private to allow changes without
breaking user code. Some unit tests still need access to internal helpers to
validate tricky edge cases (process/pipe coordination, stream handling, etc.).

This module provides a single, explicit surface for those tests so they do not
depend on incidental re-exports from public modules like ``cuprum.sh``.
"""

from __future__ import annotations

import typing as typ

from cuprum._backend import _check_rust_available
from cuprum._pipeline_internals import (
    _MIN_PIPELINE_STAGES,
    _run_before_hooks,
    _run_pipeline,
)
from cuprum._pipeline_streams import _prepare_pipeline_config, _pump_stream_dispatch
from cuprum._pipeline_wait import _PipelineWaitResult, _wait_for_pipeline
from cuprum._process_lifecycle import (
    _merge_env,
    _spawn_pipeline_processes,
    _terminate_process,
)
from cuprum._streams import (
    _READ_SIZE,
    _close_stream_writer,
    _consume_stream,
    _pump_stream,
    _StreamConfig,
    _write_chunk,
)
from cuprum.sh import _resolve_timeout

if typ.TYPE_CHECKING:
    import asyncio

    import pytest


def force_python_pump_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    """Force dispatch fallback to the Python pump and count fallback calls.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch stream-dispatch internals for the active test.

    Returns
    -------
    dict[str, int]
        Mutable counter updated each time the Python pump fallback runs.
    """
    from cuprum import _pipeline_streams

    original_pump = _pipeline_streams._pump_stream
    call_counter = {"calls": 0}

    async def counted_pump(
        reader: asyncio.StreamReader | None,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        call_counter["calls"] += 1
        await original_pump(reader, writer)

    monkeypatch.setattr(_pipeline_streams, "_extract_reader_fd", lambda _: None)
    monkeypatch.setattr(_pipeline_streams, "_extract_writer_fd", lambda _: None)
    monkeypatch.setattr(_pipeline_streams, "_pump_stream", counted_pump)
    return call_counter


_EXPORTS = {
    "force_python_pump_fallback": force_python_pump_fallback,
    "_check_rust_available": _check_rust_available,
    "_MIN_PIPELINE_STAGES": _MIN_PIPELINE_STAGES,
    "_merge_env": _merge_env,
    "_PipelineWaitResult": _PipelineWaitResult,
    "_prepare_pipeline_config": _prepare_pipeline_config,
    "_pump_stream_dispatch": _pump_stream_dispatch,
    "_run_before_hooks": _run_before_hooks,
    "_run_pipeline": _run_pipeline,
    "_spawn_pipeline_processes": _spawn_pipeline_processes,
    "_terminate_process": _terminate_process,
    "_wait_for_pipeline": _wait_for_pipeline,
    "_READ_SIZE": _READ_SIZE,
    "_close_stream_writer": _close_stream_writer,
    "_consume_stream": _consume_stream,
    "_pump_stream": _pump_stream,
    "_StreamConfig": _StreamConfig,
    "_write_chunk": _write_chunk,
    "_resolve_timeout": _resolve_timeout,
}

__all__ = list(_EXPORTS)
del _EXPORTS
