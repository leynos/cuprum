"""Parent-side tee hot-path profiling worker.

This module drives the benchmark path that consumes command output on the
parent side. It is intended to be run in an isolated subprocess by the CLI, or
called directly from benchmark harnesses that need the same worker behaviour
without an extra process boundary.

The primary public surface is ``TeeProfileWorkerConfig`` for validated worker
inputs, ``TeeProfileWorkerResult`` for the JSON-compatible result payload,
``run_tee_profile_worker`` for executing a configured run, and the
``BackendSelector`` and ``Clock`` protocols used to inject backend selection
and timing behaviour in tests.

Backend selection lives in ``benchmarks._tee_profile_worker_backend``, which
provides ``_EnvBackendSelector`` and the ``BackendSelector``/``Clock``
protocols re-exported here for existing production imports and type
annotations. That module mutates ``CUPRUM_STREAM_BACKEND`` under an ``RLock``,
rejects same-thread nested activation, and clears the ``cuprum._backend``
caches so backend discovery reflects the active environment.

Configuration and result types live in
``benchmarks._tee_profile_worker_config``, command construction lives in
``benchmarks._tee_profile_worker_command``, and execution and result assembly
live in ``benchmarks._tee_profile_worker_execution``. All are re-exported here
for existing production imports.

Command output is sent through ``benchmarks.sinks`` to exercise the same sink
families used by the benchmark suite.

The ``main()`` entry point parses CLI arguments, runs the worker, and writes a
machine-readable JSON result to stdout or to the requested output path.
"""

from __future__ import annotations

import argparse
import dataclasses as dc
import json
import logging
import pathlib as pth
import sys
import typing as typ

from benchmarks._tee_profile_worker_backend import (
    BackendName,
    BackendSelector,
    Clock,
    _default_clock,
    _EnvBackendSelector,
    _SelectorMetrics,
)
from benchmarks._tee_profile_worker_command import (
    WorkerCommandResult,
    _build_command,
    _capture_and_echo_flags,
    _catalogue_for_worker,
    _manifest_hash,
    _passthrough_script,
    _result_exit_code,
    _WorkerCommand,
    _writer_script,
)
from benchmarks._tee_profile_worker_config import (
    _MAX_REPEAT_COUNT,
    _VALID_BACKENDS,
    _VALID_MODES,
    _VALID_SINKS,
    TeeMode,
    TeeProfileWorkerConfig,
    TeeProfileWorkerResult,
    _validate_repeat_count,
)
from benchmarks._tee_profile_worker_execution import (
    _build_worker_result,
    _run_command_sync,
    _run_once,
    _run_repeat_loop,
    _RunTotals,
    _scenario_label,
    _TimingContext,
    run_tee_profile_worker,
)
from benchmarks.sinks import SinkKind, open_sink
from cuprum import (
    ExecEvent,
    ExecutionContext,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    ScopeConfig,
    scoped,
    sh,
)
from cuprum._streams_pump import _READ_SIZE, _current_read_size, _override_read_size

# Private names appear in __all__ alongside the public surface so that
# attribute access on ``benchmarks.tee_profile_worker`` keeps resolving them
# after the command/execution/config split, matching their pre-split
# availability on this module.
__all__ = [
    "_MAX_REPEAT_COUNT",
    "_READ_SIZE",
    "_VALID_BACKENDS",
    "_VALID_MODES",
    "_VALID_SINKS",
    "BackendName",
    "BackendSelector",
    "Clock",
    "ExecEvent",
    "ExecutionContext",
    "Program",
    "ProgramCatalogue",
    "ProjectSettings",
    "ScopeConfig",
    "SinkKind",
    "TeeMode",
    "TeeProfileWorkerConfig",
    "TeeProfileWorkerResult",
    "WorkerCommandResult",
    "_EnvBackendSelector",
    "_RunTotals",
    "_SelectorMetrics",
    "_TimingContext",
    "_WorkerCommand",
    "_build_command",
    "_build_worker_result",
    "_capture_and_echo_flags",
    "_catalogue_for_worker",
    "_current_read_size",
    "_default_clock",
    "_manifest_hash",
    "_override_read_size",
    "_passthrough_script",
    "_result_exit_code",
    "_run_command_sync",
    "_run_once",
    "_run_repeat_loop",
    "_scenario_label",
    "_validate_repeat_count",
    "_writer_script",
    "dc",
    "main",
    "open_sink",
    "run_tee_profile_worker",
    "scoped",
    "sh",
    "typ",
]


def _parse_args() -> argparse.Namespace:
    """Parse worker CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=pth.Path, required=True)
    parser.add_argument("--stages", type=int, required=True)
    parser.add_argument("--mode", choices=sorted(_VALID_MODES), required=True)
    parser.add_argument("--sink-kind", choices=sorted(_VALID_SINKS), required=True)
    parser.add_argument("--line-callbacks", action="store_true")
    parser.add_argument("--backend", choices=sorted(_VALID_BACKENDS), default="auto")
    parser.add_argument("--repeat-count", type=int, default=1)
    parser.add_argument("--read-size", type=int, default=_READ_SIZE)
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--errors", default="replace")
    parser.add_argument("--output", type=pth.Path)
    return parser.parse_args()


def main() -> int:
    """Run the tee profiling worker CLI.

    Returns
    -------
    int
        Process exit code derived from the worker result's ``exit_code`` field;
        0 on success.
    """
    # Developer guide: CLIs must initialize warning-level logging explicitly.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args()
    try:
        config = TeeProfileWorkerConfig(
            fixture_path=args.fixture,
            stages=args.stages,
            mode=args.mode,
            sink_kind=args.sink_kind,
            with_line_callbacks=args.line_callbacks,
            backend=args.backend,
            repeat_count=args.repeat_count,
            read_size=args.read_size,
            encoding=args.encoding,
            errors=args.errors,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    result = run_tee_profile_worker(config)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(payload)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
