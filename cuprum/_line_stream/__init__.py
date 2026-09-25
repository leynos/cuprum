"""Pull-style line iteration over one subprocess.

This package owns the coordination behind ``SafeCmd.lines()``. The submodules
split that work by responsibility:

- ``coordinator`` starts the run, waits for exit, tears down, and hands the
  result back.
- ``line_queue`` holds the bounded queue plumbing and the spawned-run record.
- ``spawn`` builds, and on failure unwinds, a run before it is handed back.
- ``drain`` drains the settled stream consumers once the child has exited.
- ``telemetry`` emits the correlated lifecycle events for one run.

Every helper is re-exported here so callers import from ``cuprum._line_stream``
regardless of which submodule defines it.
"""

from __future__ import annotations

from cuprum._line_stream.coordinator import (
    _cleanup_failed_line_stream_run,
    _coordinate_line_stream,
    _discard_drain,
    _run_line_stream_teardown,
    _run_to_command_result,
    _start_line_stream_run,
    _wait_for_line_stream_exit,
)
from cuprum._line_stream.drain import _drain_after_exit
from cuprum._line_stream.line_queue import (
    _LINE_QUEUE_CAPACITY,
    _line_event_queue,
    _LineQueueItem,
    _LineStreamRun,
    _observed_line_hook,
    _queue_line_sink,
)
from cuprum._line_stream.spawn import (
    _abandon_unstarted_run,
    _build_unstarted_run,
    _with_line_sink_hooks,
)
from cuprum._line_stream.telemetry import (
    _LineStreamEventDetails,
    _LineStreamTelemetry,
)

__all__ = [
    "_LINE_QUEUE_CAPACITY",
    "_LineQueueItem",
    "_LineStreamEventDetails",
    "_LineStreamRun",
    "_LineStreamTelemetry",
    "_abandon_unstarted_run",
    "_build_unstarted_run",
    "_cleanup_failed_line_stream_run",
    "_coordinate_line_stream",
    "_discard_drain",
    "_drain_after_exit",
    "_line_event_queue",
    "_observed_line_hook",
    "_queue_line_sink",
    "_run_line_stream_teardown",
    "_run_to_command_result",
    "_start_line_stream_run",
    "_wait_for_line_stream_exit",
    "_with_line_sink_hooks",
]
