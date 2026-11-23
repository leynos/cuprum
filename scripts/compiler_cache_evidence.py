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

- ``EVIDENCE_DIR``: directory holding ``snapshots/``, ``marks/`` and
  ``records.jsonl``.
- ``EVIDENCE_BRACKET``: the bracketed step's label, such as ``typecheck``.
- ``EVIDENCE_PHASE``: ``pre`` or ``post`` for ``snapshot``.
- ``EVIDENCE_MARKER``: ``restore`` or ``save`` for ``mark``.
- ``EVIDENCE_EXPECTS_RUSTC``: ``true`` when the bracket should compile.
- ``EVIDENCE_TARGET_PRESENT``: ``true`` when a Cargo ``target`` directory
  already existed when the bracket opened.
- ``EVIDENCE_FAMILY``, ``EVIDENCE_MATRIX_LEG``, ``EVIDENCE_COMPILER_SHAPE``,
  ``EVIDENCE_PYTHON_VERSION``, ``EVIDENCE_TOOLCHAIN``, ``EVIDENCE_DEV_FAST``,
  ``EVIDENCE_MATCHED_KEY``, ``EVIDENCE_RESTORE_HIT``,
  ``EVIDENCE_SETUP_STATUS``, ``EVIDENCE_PR_NUMBER``: identity fields.
- The run identity comes from ``GITHUB_RUN_ID``, ``GITHUB_RUN_ATTEMPT``,
  ``GITHUB_JOB``, ``GITHUB_EVENT_NAME``, and the workflow file name parsed out
  of ``GITHUB_WORKFLOW_REF``.
- ``RUSTC_WRAPPER`` is read for presence only, and ``ACTIONS_CACHE_URL`` is
  read to classify its host, never to record it.

The schema and pure classifiers live in ``scripts.compiler_cache_schema``; the
direct probes live in ``scripts.compiler_cache_probe``; the validating reader
lives in ``scripts.compiler_cache_summary``.
"""

from __future__ import annotations

import json
import os
import sys
import typing as typ
from pathlib import Path

from scripts import compiler_cache_probe as probe
from scripts import compiler_cache_schema as schema

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_WARNING = "::warning title=compiler-cache-evidence::"


def capture(environ: cabc.Mapping[str, str]) -> dict[str, object]:
    """Build one snapshot from sccache and the bracket's declared identity.

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
    stats, version, backend = probe.read_stats(environ)
    available = bool(stats)
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "bracket": bracket,
        "phase": phase,
        "captured_at": schema.now(),
        "available": available,
        "restore_seconds": probe.elapsed_between(environ, "restore", "restore-end"),
        "restore_bytes": probe.directory_bytes(environ),
        "sccache_version": version or None,
        "cache_backend": backend if available else "none",
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


def _not_cached_delta(
    pre: cabc.Mapping[str, object], post: cabc.Mapping[str, object]
) -> dict[str, int]:
    """Return per-reason deltas, treating an absent reason as zero.

    A reason that first appears in the post snapshot is a real count for this
    bracket, not an unknown value. Only an unavailable snapshot leaves the map
    unknown, which the caller represents as an empty mapping.

    Returns
    -------
    dict[str, int]
        One delta per reason seen in either snapshot, or an empty mapping when
        a snapshot is unavailable.
    """
    if not pre.get("available") or not post.get("available"):
        return {}
    before = typ.cast("dict[str, object]", pre.get("not_cached", {}))
    after = typ.cast("dict[str, object]", post.get("not_cached", {}))
    reasons = set(before) | set(after)
    return {
        str(reason): int(typ.cast("int", after.get(reason, 0)))
        - int(typ.cast("int", before.get(reason, 0)))
        for reason in reasons
    }


def _snapshot_path(environ: cabc.Mapping[str, str], bracket: str, phase: str) -> Path:
    """Return the path a snapshot is stored at."""
    return Path(environ["EVIDENCE_DIR"]) / "snapshots" / f"{bracket}-{phase}.json"


def mark(environ: cabc.Mapping[str, str]) -> int:
    """Write a wall-clock marker before a restore or save; never fail the job.

    Returns
    -------
    int
        Always zero. A missing marker makes one timing field ``unknown``; it
        must not fail the step that measures a paid transfer.
    """
    marker = environ.get("EVIDENCE_MARKER", "")
    if marker not in {"restore", "restore-end", "save", "save-end"}:
        print(f"{_WARNING}Invalid timing marker; the marker was omitted.")
        return 0
    if not probe.write_mark(environ, marker):
        print(f"{_WARNING}Could not persist the timing marker.")
    return 0


def snapshot(environ: cabc.Mapping[str, str]) -> int:
    """Write one snapshot; never fail the job for a missing cache server.

    Returns
    -------
    int
        Zero when the snapshot was persisted. A missing sccache still writes a
        snapshot carrying ``available: false``; an unusable bracket or phase,
        or an unwritable directory, is reported and also returns zero.
    """
    try:
        state = capture(environ)
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
    """Return one snapshot.

    Returns
    -------
    dict[str, object] | None
        The parsed snapshot, or ``None`` when the file is absent, unreadable,
        or not a JSON object.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return (
        typ.cast("dict[str, object]", document) if isinstance(document, dict) else None
    )


def _state(record: dict[str, object]) -> str:
    """Classify one record as ok, degraded, or noncompliant.

    A fallback or an absent wrapper means the job compiled without the cache,
    which is degraded but not a wiring fault. A ``gha`` backend reached while
    the v2 service flag is set, or through a GitHub-hosted cache address, is
    the wrong store and is noncompliant.

    Returns
    -------
    str
        One of ``ok``, ``degraded``, or ``noncompliant``.
    """
    wrong_store = record["cache_service_v2"] or record["cache_host_class"] in {
        "github-results",
        "github-legacy",
    }
    if record["backend"] == "gha" and wrong_store:
        return "noncompliant"
    if record["setup_status"] != "started" or not record["wrapper_present"]:
        return "degraded"
    if record["zero_requests"] == "unexplained":
        return "degraded"
    return "ok"


def _identity_fields(
    environ: cabc.Mapping[str, str],
    bracket: str,
    identity: tuple[str, str, str],
    post: cabc.Mapping[str, object],
) -> dict[str, object]:
    """Return the record's identity and provenance fields."""
    run_id, run_attempt, job = identity
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "record_kind": schema.RECORD_KIND,
        "bracket": bracket,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "job": job,
        "workflow": schema.workflow_name(environ.get("GITHUB_WORKFLOW_REF")),
        "event_class": schema.event_class(environ.get("GITHUB_EVENT_NAME")),
        "pr_number": schema.bounded(environ.get("EVIDENCE_PR_NUMBER"), schema.DECIMAL),
        "recorded_at": schema.now(),
        "setup_status": schema.bounded(environ.get("EVIDENCE_SETUP_STATUS")),
        "backend": schema.bounded(post.get("cache_backend")),
        "wrapper_present": bool(environ.get("RUSTC_WRAPPER")),
        "cache_service_v2": bool(post.get("cache_service_v2")),
        "cache_host_class": schema.bounded(post.get("cache_host_class")),
        "sccache_version": schema.bounded(post.get("sccache_version")),
        "rustc": probe.rustc(environ),
        "family": schema.bounded(environ.get("EVIDENCE_FAMILY")),
        "matrix_leg": schema.bounded(environ.get("EVIDENCE_MATRIX_LEG")),
        "compiler_shape": schema.bounded(environ.get("EVIDENCE_COMPILER_SHAPE")),
        "python_version": schema.bounded(
            environ.get("EVIDENCE_PYTHON_VERSION"), schema.PYTHON_VERSION
        ),
        "toolchain": schema.bounded(environ.get("EVIDENCE_TOOLCHAIN")),
        "dev_fast": schema.boolean(environ.get("EVIDENCE_DEV_FAST")),
        "checkout_sha": schema.sha(environ.get("EVIDENCE_CHECKOUT_SHA")),
        "head_sha": schema.sha(environ.get("EVIDENCE_HEAD_SHA")),
        "base_sha": schema.sha(environ.get("EVIDENCE_BASE_SHA")),
        "matched_key": schema.bounded(environ.get("EVIDENCE_MATCHED_KEY")),
        "restore_hit": schema.boolean(environ.get("EVIDENCE_RESTORE_HIT")),
    }


def build_record(environ: cabc.Mapping[str, str]) -> dict[str, object]:
    """Combine a bracket's snapshots and identity into one evidence record.

    Parameters
    ----------
    environ : collections.abc.Mapping[str, str]
        The step environment described in the module docstring.

    Returns
    -------
    dict[str, object]
        The schema-version-1 record, ready to encode as one JSON line.

    Raises
    ------
    ValueError
        If a snapshot is missing, the run identity is malformed, or a declared
        closed-vocabulary field is outside its vocabulary.
    """
    bracket = schema.bounded(environ.get("EVIDENCE_BRACKET"), schema.BRACKET)
    if bracket == "unknown":
        msg = "invalid bracket"
        raise ValueError(msg)
    pre = _read_snapshot(_snapshot_path(environ, bracket, "pre"))
    post = _read_snapshot(_snapshot_path(environ, bracket, "post"))
    if pre is None or post is None:
        msg = "missing snapshot"
        raise ValueError(msg)
    run_id = environ.get("GITHUB_RUN_ID", "")
    run_attempt = environ.get("GITHUB_RUN_ATTEMPT", "")
    job = schema.bounded(environ.get("GITHUB_JOB"))
    if not schema.is_decimal(run_id) or not schema.is_decimal(run_attempt):
        msg = "invalid run identity"
        raise ValueError(msg)
    if job == "unknown":
        msg = "invalid job"
        raise ValueError(msg)
    counters = schema.delta_map(
        typ.cast("dict[str, object]", post["counters"]),
        typ.cast("dict[str, object]", pre["counters"]),
    )
    requests = counters.get("compile_requests")
    record = _identity_fields(environ, bracket, (run_id, run_attempt, job), post)
    record.update({
        "restore_seconds": pre.get("restore_seconds"),
        "restore_bytes": pre.get("restore_bytes"),
        "save_seconds": probe.elapsed_between(environ, "save", "save-end"),
        "save_bytes": probe.directory_bytes(environ),
        "counters": counters,
        "durations": schema.delta_map(
            typ.cast("dict[str, object]", post["durations"]),
            typ.cast("dict[str, object]", pre["durations"]),
        ),
        "not_cached": _not_cached_delta(pre, post),
        "zero_requests": schema.zero_requests_kind(
            requests if isinstance(requests, int) else None,
            expects_rustc=schema.boolean(environ.get("EVIDENCE_EXPECTS_RUSTC")),
            target_present=schema.boolean(environ.get("EVIDENCE_TARGET_PRESENT")),
        ),
    })
    _normalize(record)
    record["state"] = _state(record)
    return record


def _normalize(record: dict[str, object]) -> None:
    """Replace an out-of-vocabulary label with its declared fallback."""
    fallbacks = {
        "setup_status": ("absent", schema.SETUP_STATUSES),
        "compiler_shape": ("unknown", schema.COMPILER_SHAPES),
        "backend": ("unknown", schema.BACKENDS),
        "cache_host_class": ("absent", schema.CACHE_HOSTS),
    }
    for name, (fallback, vocabulary) in fallbacks.items():
        if record[name] not in vocabulary:
            record[name] = fallback


def record(environ: cabc.Mapping[str, str]) -> int:
    """Append one record; fail open with a fixed warning on invalid input.

    Returns
    -------
    int
        Always zero. Telemetry must not fail a build that compiled correctly,
        and an invalid record is reported rather than persisted.
    """
    try:
        document = build_record(environ)
        encoded = json.dumps(document, separators=(",", ":"))
        path = Path(environ["EVIDENCE_DIR"]) / "records.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as records:
            records.write(encoded + "\n")
    except (ValueError, KeyError, OSError):
        print(f"{_WARNING}Invalid or unwritable evidence fields; record omitted.")
    return 0


def main(argv: list[str], environ: cabc.Mapping[str, str]) -> int:
    """Dispatch one writer mode and return its exit status."""
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "snapshot":
        return snapshot(environ)
    if mode == "record":
        return record(environ)
    if mode == "mark":
        return mark(environ)
    print(f"usage: {Path(argv[0]).name} snapshot|record|mark", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
