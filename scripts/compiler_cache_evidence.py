#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Entry point for the compiler-cache evidence writer.

The writer records observations, not verdicts. A snapshot stores one bracket's
allowlisted sccache statistics; a record subtracts the bracket's ``pre``
snapshot from its ``post`` snapshot and appends one JSON line; a marker times a
cache restore or save so the record can charge the transfer a migration would
remove.

The work is split by responsibility: ``scripts.compiler_cache_snapshots``
captures and reads snapshots, ``scripts.compiler_cache_record`` assembles a
record from an already-read observation bundle, and
``scripts.compiler_cache_commands`` runs the commands and maps their typed
outcomes to warnings and exit statuses. This module is the checked-in script
entry and keeps those names importable for the tests and the composite action.

The inputs arrive as environment variables so the composite action's ``env``
block is the whole contract:

- ``EVIDENCE_DIR``: holds ``snapshots/``, ``marks/`` and ``records.jsonl``.
- ``EVIDENCE_BRACKET``: the bracketed step's label, such as ``typecheck``.
- ``EVIDENCE_PHASE``: ``pre`` or ``post``; ``EVIDENCE_MARKER``: ``restore``
  or ``save``.
- ``EVIDENCE_EXPECTS_RUSTC``: ``true`` when the bracket should compile;
  ``EVIDENCE_TARGET_PRESENT``: ``true`` when a Cargo ``target`` already existed.
- ``EVIDENCE_FAMILY``, ``EVIDENCE_MATRIX_LEG``, ``EVIDENCE_RUNNER_CLASS``,
  ``EVIDENCE_COMPILER_SHAPE``, ``EVIDENCE_PYTHON_VERSION``,
  ``EVIDENCE_TOOLCHAIN``, ``EVIDENCE_DEV_FAST``, ``EVIDENCE_MATCHED_KEY``,
  ``EVIDENCE_RESTORE_HIT``, ``EVIDENCE_SETUP_STATUS``, ``EVIDENCE_PR_NUMBER``:
  identity fields.
- The run identity comes from ``GITHUB_RUN_ID``, ``GITHUB_RUN_ATTEMPT``,
  ``GITHUB_JOB``, ``GITHUB_EVENT_NAME``, and the workflow file name parsed out
  of ``GITHUB_WORKFLOW_REF``.
- ``RUSTC_WRAPPER`` is read for presence only, and ``ACTIONS_CACHE_URL`` is
  read to classify its host, never to record it.
"""

from __future__ import annotations

import os
import sys

from scripts.compiler_cache_commands import (
    CommandResult,
    WriterEdge,
    append_text_file,
    gather_observation,
    main,
    mark,
    record,
    snapshot,
    write_mark,
    write_text_file,
)
from scripts.compiler_cache_record import ObservationBundle, build_record
from scripts.compiler_cache_snapshots import SnapshotEdge, capture

__all__ = [
    "CommandResult",
    "ObservationBundle",
    "SnapshotEdge",
    "WriterEdge",
    "append_text_file",
    "build_record",
    "capture",
    "gather_observation",
    "main",
    "mark",
    "record",
    "snapshot",
    "write_mark",
    "write_text_file",
]

if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
