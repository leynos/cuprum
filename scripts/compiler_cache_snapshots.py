"""Capture and read compiler-cache snapshots.

A snapshot is one bracket's allowlisted sccache statistics plus the identity
that produced them. This module owns the snapshot's path, its JSON shape, and
the reads that turn it back into a record's pre and post values. The record
and command edges live in ``compiler_cache_commands``.
"""

from __future__ import annotations

import collections.abc as cabc
import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell
import typing as typ
from pathlib import Path

from scripts import compiler_cache_probe as probe
from scripts import compiler_cache_schema as schema

type Clock = cabc.Callable[[], str]

#: The mappings a snapshot must carry before a record can use it.
SNAPSHOT_FIELDS = ("counters", "durations", "not_cached")


class SnapshotEdge(typ.NamedTuple):
    """The clock, runner, and filesystem seams a snapshot is read through."""

    clock: Clock = schema.now
    runner: probe.Runner = subprocess.run
    scan: probe.Scan = probe.directory_scan
    read: probe.ReadText = probe.read_text
    stat_path: probe.PathStat = Path.stat


_DEFAULT_EDGE = SnapshotEdge()


def capture(
    environ: cabc.Mapping[str, str], *, edge: SnapshotEdge = _DEFAULT_EDGE
) -> dict[str, object]:
    """Build one snapshot from sccache and the bracket's declared identity.

    This is the snapshot edge: it runs ``sccache`` through ``runner``, reads
    the restore markers through ``read``, and measures the local cache through
    ``scan`` and ``stat_path``.

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
    observed = probe.read_stats(environ, runner=edge.runner)
    stats = observed.stats
    available = observed.ok
    restore_span = probe.elapsed_between(
        environ, "restore", "restore-end", read=edge.read
    )
    restore_size = probe.directory_bytes(
        environ, scan=edge.scan, stat_path=edge.stat_path
    )
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "bracket": bracket,
        "phase": phase,
        "captured_at": edge.clock(),
        "available": available,
        "restore_seconds": restore_span.seconds,
        "restore_seconds_detail": restore_span.detail,
        "restore_bytes": restore_size.size,
        "restore_bytes_detail": restore_size.detail,
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


def snapshot_path(environ: cabc.Mapping[str, str], bracket: str, phase: str) -> Path:
    """Return the path a snapshot is stored at."""
    return Path(environ["EVIDENCE_DIR"]) / "snapshots" / f"{bracket}-{phase}.json"


def read_snapshot(path: Path) -> dict[str, object] | None:
    """Return the parsed snapshot, or ``None`` when it is unusable."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return (
        typ.cast("dict[str, object]", document) if isinstance(document, dict) else None
    )


def load_bracket(
    environ: cabc.Mapping[str, str],
) -> tuple[str, dict[str, object], dict[str, object]]:
    """Return the bracket and its snapshots, refusing a malformed snapshot.

    A document that lacks one of ``SNAPSHOT_FIELDS`` is reported as invalid
    evidence, not as a missing directory, so the command's warning names the
    right cause.

    Returns
    -------
    tuple[str, dict[str, object], dict[str, object]]
        The bracket label and its pre and post snapshots.

    Raises
    ------
    ValueError
        If the bracket is invalid, a snapshot is missing, or a snapshot lacks
        a required mapping.
    """
    bracket = schema.bounded(environ.get("EVIDENCE_BRACKET"), schema.BRACKET)
    if bracket == "unknown":
        msg = "invalid bracket"
        raise ValueError(msg)
    pre = read_snapshot(snapshot_path(environ, bracket, "pre"))
    post = read_snapshot(snapshot_path(environ, bracket, "post"))
    if pre is None or post is None:
        msg = "missing snapshot"
        raise ValueError(msg)
    for snapshot in (pre, post):
        if any(not isinstance(snapshot.get(name), dict) for name in SNAPSHOT_FIELDS):
            msg = "snapshot is missing required fields"
            raise ValueError(msg)
    return bracket, pre, post
