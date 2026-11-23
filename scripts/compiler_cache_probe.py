"""Probe the local compiler cache and report only allowlisted facts.

The evidence writer needs four things the runner can observe directly: the
sccache statistics document, the compiler's identity, the size of the local
cache directory a migration would remove, and the wall time since a restore or
save marker. Each probe fails open and returns ``None`` or an ``unknown``
marker, so a job that compiled correctly is never failed by a missing probe.

The statistics document is reduced to the allowlisted counters and durations
before it leaves this module. The raw document also carries the cache location,
which can name a runner path or a service address; that value is classified by
``scripts.compiler_cache_schema`` and never returned.

Examples
--------
>>> count({"counts": {"Rust": 3}, "adv_counts": {}})
3
>>> duration({"secs": 1, "nanos": 500_000_000})
1.5
"""

from __future__ import annotations

import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell
import time
import typing as typ
from pathlib import Path

from scripts import compiler_cache_schema as schema

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_EMPTY: dict[str, object] = {}


def read_stats(environ: cabc.Mapping[str, str]) -> tuple[dict[str, object], str, str]:
    """Run sccache and return its statistics, version, and classified store.

    The statistics document's top-level ``cache_location`` can name a runner
    path or a service address, so it is classified here and the raw value is
    discarded before the caller sees it.

    Returns
    -------
    tuple[dict[str, object], str, str]
        The parsed ``stats`` object, the reported version, and the backend
        label. All are empty or ``none`` when sccache is absent or cannot be
        run, which the caller records as unavailable rather than as zeroes.
    """
    binary = environ.get("SCCACHE_BINARY", "sccache")
    try:
        completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
            [binary, "--show-stats", "--stats-format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return {}, "", "none"
        document = json.loads(completed.stdout)
        stats = document.get("stats", _EMPTY)
        version = str(document.get("version", ""))
        backend = schema.backend_of(document.get("cache_location"))
        if not isinstance(stats, dict):
            return {}, version, backend
        return typ.cast("dict[str, object]", stats), version, backend
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return {}, "", "none"


def count(mapping: object) -> int | None:
    """Sum the per-language ``counts`` map sccache reports.

    sccache increments both ``counts`` and ``adv_counts`` for each event, so
    adding both maps would double every hit and miss. Its own ``all()`` reads
    ``counts`` alone, which is the count this writer records.

    Returns
    -------
    int | None
        The summed count, or ``None`` when the document has no per-language
        counts map.
    """
    if not isinstance(mapping, dict) or "counts" not in mapping:
        return None
    values = typ.cast("dict[str, object]", mapping)["counts"]
    if not isinstance(values, dict):
        return None
    return sum(
        value
        for value in typ.cast("dict[str, object]", values).values()
        if isinstance(value, int) and not isinstance(value, bool)
    )


def counter(stats: cabc.Mapping[str, object], name: str) -> int | None:
    """Return one counter, summing the per-language maps sccache uses."""
    if name in {"cache_hits", "cache_misses"}:
        return count(stats.get(name))
    value = stats.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def duration(value: object) -> float | None:
    """Return one ``{secs, nanos}`` duration in seconds."""
    if not isinstance(value, dict):
        return None
    seconds = typ.cast("dict[str, object]", value).get("secs")
    nanos = typ.cast("dict[str, object]", value).get("nanos")
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        return None
    fraction = nanos if isinstance(nanos, int) and not isinstance(nanos, bool) else 0
    return float(seconds) + fraction / 1_000_000_000


def not_cached(stats: cabc.Mapping[str, object]) -> dict[str, int]:
    """Return the non-cacheable reasons with their counts, bounded by name."""
    reasons = stats.get("not_cached")
    if not isinstance(reasons, dict):
        return {}
    result: dict[str, int] = {}
    for name, value in typ.cast("dict[str, object]", reasons).items():
        if not schema.REASON.fullmatch(str(name)):
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            result[str(name)] = value
    return result


def directory_bytes(environ: cabc.Mapping[str, str]) -> int | None:
    """Return the local compiler directory's size in bytes, or ``None``.

    The directory is the archive the direct backend would replace, so its size
    is the transfer the migration removes. An absent directory, as on a
    ``gha`` backend or a fork run, has no size to report.

    Returns
    -------
    int | None
        The summed file sizes, or ``None`` when the directory is absent or
        unreadable.
    """
    root = Path(environ.get("HOME", "")) / ".cache" / "sccache"
    if not root.is_dir():
        return None
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    except OSError:
        return None
    return total


def rustc(environ: cabc.Mapping[str, str]) -> dict[str, str]:
    """Return the compiler's release, commit, and host, or ``unknown``."""
    binary = environ.get("RUSTC_BINARY", "rustc")
    values = {"release": "unknown", "commit": "unknown", "host": "unknown"}
    try:
        completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
            [binary, "-vV"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return values
    if completed.returncode != 0:
        return values
    for line in completed.stdout.splitlines():
        name, _, value = line.partition(": ")
        if name == "commit-hash":
            name = "commit"
        if name in values and value:
            values[name] = value
    return values


def mark_path(environ: cabc.Mapping[str, str], marker: str) -> Path:
    """Return the path a timing marker is stored at."""
    return Path(environ["EVIDENCE_DIR"]) / "marks" / f"{marker}-start"


def _marker_file(environ: cabc.Mapping[str, str], name: str) -> Path:
    """Return the stored file for a marker name, without a suffix."""
    return Path(environ["EVIDENCE_DIR"]) / "marks" / name


def seconds_since(environ: cabc.Mapping[str, str], marker: str) -> float | None:
    """Return seconds elapsed since a marker, or ``None`` when it is absent."""
    try:
        started = float(mark_path(environ, marker).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    elapsed = time.time() - started
    return round(elapsed, 3) if elapsed >= 0 else None


def elapsed_between(
    environ: cabc.Mapping[str, str], start: str, end: str
) -> float | None:
    """Return seconds between two markers, or ``None`` when either is absent.

    Returns
    -------
    float | None
        The non-negative interval, or ``None`` when a marker is missing or
        unreadable.
    """
    begin_path = mark_path(environ, start)
    finish_path = _marker_file(environ, end)
    try:
        begin = float(begin_path.read_text(encoding="utf-8").strip())
        finish = float(finish_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    elapsed = finish - begin
    return round(elapsed, 3) if elapsed >= 0 else None


def write_mark(environ: cabc.Mapping[str, str], marker: str) -> bool:
    """Write one wall-clock marker.

    Returns
    -------
    bool
        Whether the marker was persisted. A failure leaves one timing field
        unknown, which never fails the step that measures a paid transfer.
    """
    path = (
        _marker_file(environ, marker)
        if marker in {"restore-end", "save-end"}
        else mark_path(environ, marker)
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{time.time():.6f}\n", encoding="utf-8")
    except OSError:
        return False
    return True
