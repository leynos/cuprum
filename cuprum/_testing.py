"""Test-only re-exports of internal helpers.

Cuprum keeps most implementation details private to allow changes without
breaking user code. Some unit tests still need access to internal helpers to
validate tricky edge cases (process/pipe coordination, stream handling, etc.).

This module provides a single, explicit surface for those tests so they do not
depend on incidental re-exports from public modules like ``cuprum.sh``.
"""

from __future__ import annotations

from cuprum._pipeline_internals import (
    _MIN_PIPELINE_STAGES,
    _run_before_hooks,
    _run_pipeline,
)
from cuprum._pipeline_streams import _prepare_pipeline_config
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

_EXPORTS = {
    "_MIN_PIPELINE_STAGES": _MIN_PIPELINE_STAGES,
    "_merge_env": _merge_env,
    "_PipelineWaitResult": _PipelineWaitResult,
    "_prepare_pipeline_config": _prepare_pipeline_config,
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
}

__all__ = list(_EXPORTS)
del _EXPORTS
