#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Record one bracket of sccache statistics as a bounded evidence line.

The compiler-cache backend experiment needs per-job observations from ordinary
CI runs. A green job proves nothing about compiler reuse: sccache reports a
plausible hit rate whatever backend it bound, a restored archive can contain
nothing a job can use, and a route that never invokes rustc reports zero
requests without losing anything.

The writer therefore records observations, not verdicts. ``snapshot`` stores an
allowlisted set of counters under a bracket label; ``record`` subtracts the
bracket's ``pre`` snapshot from its ``post`` snapshot and appends one JSON line;
``mark`` writes a wall-clock marker before a cache restore or save so the
record can charge the transfer a migration would remove.

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

The schema and classifiers live in ``scripts.compiler_cache_schema``, the
probes in ``scripts.compiler_cache_probe``, the reader in ``..._summary``.
"""

from __future__ import annotations

import collections.abc as cabc
import json
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell
import sys
import time
import typing as typ
from pathlib import Path

from scripts import compiler_cache_probe as probe
from scripts import compiler_cache_schema as schema
from scripts.compiler_cache_record import (
    ObservationBundle,
    build_record,
    identity_fields,
    run_identity,
)
from scripts.compiler_cache_validation import validate_record

_WARNING = "::warning title=compiler-cache-evidence::"
type Clock = cabc.Callable[[], str]
type WallClock = cabc.Callable[[], float]


def capture(
    environ: cabc.Mapping[str, str],
    *,
    clock: Clock = schema.now,
    runner: probe.Runner = subprocess.run,
) -> dict[str, object]:
    """Build one snapshot from sccache and the bracket's declared identity.

    This is the probe edge: it runs ``sccache`` through ``runner``, reads the
    restore markers, and measures the local cache directory.

    Parameters
    ----------
    environ : collections.abc.Mapping[str, str]
        The step environment described in the module docstring.

    Returns
    -------
    dict[str, object]
        A snapshot with allowlisted counters only. ``available`` is false when
        sccache could not be read, and every counter is then ``null``.

    Raises
    ------
    ValueError
        If the bracket or phase is missing or malformed.
    """
    bracket = environ.get("EVIDENCE_BRACKET", "")
    phase = environ.get("EVIDENCE_PHASE", "")
    if not schema.BRACKET.fullmatch(bracket):
        msg = "invalid bracket"
        raise ValueError(msg)
    if phase not in {"pre", "post"}:
        msg = "phase must be pre or post"
        raise ValueError(msg)
    observed = probe.read_stats(environ, runner=runner)
    stats = observed.stats
    available = observed.ok
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "bracket": bracket,
        "phase": phase,
        "captured_at": clock(),
        "available": available,
        "restore_seconds": probe.elapsed_between(
            environ, "restore", "restore-end"
        ).seconds,
        "restore_bytes": probe.directory_bytes(environ).size,
        "sccache_version": observed.version or None,
        "cache_backend": observed.backend if available else "none",
        "cache_service_v2": "ACTIONS_CACHE_SERVICE_V2" in environ,
        "cache_host_class": schema.cache_host_class(environ.get("ACTIONS_CACHE_URL")),
        "counters": {
            name: probe.counter(stats, name) if available else None
            for name in schema.COUNTERS
        },
        "durations": {
            name: probe.duration(stats.get(name)) if available else None
            for name in schema.DURATIONS
        },
        "not_cached": probe.not_cached(stats) if available else {},
    }


def _snapshot_path(environ: cabc.Mapping[str, str], bracket: str, phase: str) -> Path:
    """Return the path a snapshot is stored at."""
    return Path(environ["EVIDENCE_DIR"]) / "snapshots" / f"{bracket}-{phase}.json"


def mark(environ: cabc.Mapping[str, str], *, clock: WallClock = time.time) -> int:
    """Write a wall-clock marker before a restore or save; never fail the job.

    Returns
    -------
    int
        Always zero; a missing marker leaves one timing unknown, never fails.
    """
    marker = environ.get("EVIDENCE_MARKER", "")
    if marker not in {"restore", "restore-end", "save", "save-end"}:
        print(f"{_WARNING}Invalid timing marker; the marker was omitted.")
        return 0
    if not probe.write_mark(environ, marker, clock=clock):
        print(f"{_WARNING}Could not persist the timing marker.")
    return 0


def snapshot(
    environ: cabc.Mapping[str, str],
    *,
    clock: Clock = schema.now,
    runner: probe.Runner = subprocess.run,
) -> int:
    """Write one snapshot; never fail the job for a missing cache server.

    Returns
    -------
    int
        Zero when persisted. A missing sccache writes ``available: false``; an
        unusable bracket or an unwritable directory is reported, also zero.
    """
    try:
        state = capture(environ, clock=clock, runner=runner)
    except ValueError:
        print(f"{_WARNING}Invalid snapshot fields; the snapshot was omitted.")
        return 0
    path = _snapshot_path(environ, str(state["bracket"]), str(state["phase"]))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, separators=(",", ":")) + "\n", encoding="utf-8"
        )
    except OSError:
        print(f"{_WARNING}Could not persist the compiler-cache snapshot.")
    return 0


def _read_snapshot(path: Path) -> dict[str, object] | None:
    """Return the parsed snapshot, or ``None`` when it is unusable."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return (
        typ.cast("dict[str, object]", document) if isinstance(document, dict) else None
    )


def _load_bracket(
    environ: cabc.Mapping[str, str],
) -> tuple[str, dict[str, object], dict[str, object]]:
    """Return the bracket label and its two snapshots."""
    bracket = schema.bounded(environ.get("EVIDENCE_BRACKET"), schema.BRACKET)
    if bracket == "unknown":
        msg = "invalid bracket"
        raise ValueError(msg)
    pre = _read_snapshot(_snapshot_path(environ, bracket, "pre"))
    post = _read_snapshot(_snapshot_path(environ, bracket, "post"))
    if pre is None or post is None:
        msg = "missing snapshot"
        raise ValueError(msg)
    return bracket, pre, post


def gather_observation(
    environ: cabc.Mapping[str, str],
    *,
    clock: Clock = schema.now,
    runner: probe.Runner = subprocess.run,
) -> ObservationBundle:
    """Read every snapshot, marker, and probe one record needs.

    This is the writer's only read path for a record; ``build_record`` is pure.

    Returns
    -------
    ObservationBundle
        The identity fields, both snapshots, and the probed save timings.

    Raises
    ------
    ValueError
        If a snapshot is missing or the run identity is malformed.
    """  # ruff: ignore[docstring-extraneous-exception] - `_load_bracket` and `run_identity` raise ValueError.
    bracket, pre, post = _load_bracket(environ)
    identity = run_identity(environ)
    fields = identity_fields(environ, bracket, identity, post)
    compiler = probe.rustc(environ, runner=runner)
    fields["recorded_at"] = clock()
    fields["rustc"] = {
        "release": compiler.release,
        "commit": compiler.commit,
        "host": compiler.host,
    }
    return ObservationBundle(
        fields=fields,
        pre=pre,
        post=post,
        save_seconds=probe.elapsed_between(environ, "save", "save-end").seconds,
        save_bytes=probe.directory_bytes(environ).size,
        expects_rustc=schema.boolean(environ.get("EVIDENCE_EXPECTS_RUSTC")),
        target_present=schema.boolean(environ.get("EVIDENCE_TARGET_PRESENT")),
    )


def record(
    environ: cabc.Mapping[str, str],
    *,
    clock: Clock = schema.now,
    runner: probe.Runner = subprocess.run,
) -> int:
    """Append one record; fail open with a fixed warning on invalid input.

    Returns
    -------
    int
        Always zero; an invalid record is reported, never persisted.
    """
    try:
        # One malformed line makes the reader reject the whole artefact.
        document = validate_record(
            build_record(gather_observation(environ, clock=clock, runner=runner))
        )
        encoded = json.dumps(document, separators=(",", ":"))
        path = Path(environ["EVIDENCE_DIR"]) / "records.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as records:
            records.write(encoded + "\n")
    except (ValueError, KeyError, OSError):
        print(f"{_WARNING}Invalid or unwritable evidence fields; record omitted.")
    return 0


class WriterEdge(typ.NamedTuple):
    """The clocks and process runner a caller may pin at the command edge."""

    clock: Clock = schema.now
    wall_clock: WallClock = time.time
    runner: probe.Runner = subprocess.run


_DEFAULT_EDGE = WriterEdge()


def main(
    argv: list[str],
    environ: cabc.Mapping[str, str],
    *,
    edge: WriterEdge = _DEFAULT_EDGE,
) -> int:
    """Dispatch one writer mode and return its exit status."""
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "snapshot":
        return snapshot(environ, clock=edge.clock, runner=edge.runner)
    if mode == "record":
        return record(environ, clock=edge.clock, runner=edge.runner)
    if mode == "mark":
        return mark(environ, clock=edge.wall_clock)
    print(f"usage: {Path(argv[0]).name} snapshot|record|mark", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
