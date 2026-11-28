"""Probe the local compiler cache and report only allowlisted facts.

The evidence writer needs four things the runner can observe directly: the
sccache statistics document, the compiler's identity, the size of the local
cache directory a migration would remove, and the wall time since a restore or
save marker. Each probe fails open and reports an explicit ``detail`` alongside
its value, so a caller can tell an absent source from an unreadable one, and a
job that compiled correctly is never failed by a missing probe.

The statistics document is reduced to the allowlisted counters and durations
before it leaves this module. The raw document also carries the cache location,
which can name a runner path or a service address; that value is classified by
``scripts.compiler_cache_schema`` and never returned.

Process execution is a narrow injected runner, not a process-wide seam: every
test passes its own fake, and the action's default is ``subprocess.run``.

Examples
--------
>>> count({"counts": {"Rust": 3}, "adv_counts": {}})
3
>>> duration({"secs": 1, "nanos": 500_000_000})
1.5
"""

from __future__ import annotations

import json
import os
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell
import time
import typing as typ
from pathlib import Path

from scripts import compiler_cache_schema as schema

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type Runner = cabc.Callable[..., subprocess.CompletedProcess[str]]


class StatsProbe(typ.NamedTuple):
    """One sccache statistics read and whether it succeeded."""

    stats: dict[str, object]
    version: str
    backend: str
    ok: bool


class RustcIdentity(typ.NamedTuple):
    """The compiler's identity and whether it could be read."""

    release: str
    commit: str
    host: str
    ok: bool


class SizeProbe(typ.NamedTuple):
    """A directory size and why it is missing when it is."""

    size: int | None
    detail: str


class SpanProbe(typ.NamedTuple):
    """A marker interval and why it is missing when it is."""

    seconds: float | None
    detail: str


def read_stats(
    environ: cabc.Mapping[str, str], *, runner: Runner = subprocess.run
) -> StatsProbe:
    """Run sccache through ``runner`` and return its statistics and store.

    The statistics document's top-level ``cache_location`` can name a runner
    path or a service address, so it is classified here and the raw value is
    discarded before the caller sees it.

    Parameters
    ----------
    environ : collections.abc.Mapping[str, str]
        The step environment; ``SCCACHE_BINARY`` overrides the binary.
    runner : Runner
        The process runner, defaulting to ``subprocess.run``.

    Returns
    -------
    StatsProbe
        The parsed ``stats`` object, the reported version, the backend label,
        and whether the read succeeded. A failed read carries ``ok=False``,
        which the caller records as unavailable rather than as zeroes.
    """
    binary = environ.get("SCCACHE_BINARY", "sccache")
    try:
        completed = runner(
            [binary, "--show-stats", "--stats-format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return StatsProbe(stats={}, version="", backend="none", ok=False)
        document = json.loads(completed.stdout)
        version = str(document.get("version", ""))
        backend = schema.backend_of(document.get("cache_location"))
        stats = document.get("stats")
        if not isinstance(stats, dict):
            return StatsProbe(stats={}, version=version, backend=backend, ok=False)
        return StatsProbe(
            stats=typ.cast("dict[str, object]", stats),
            version=version,
            backend=backend,
            ok=True,
        )
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return StatsProbe(stats={}, version="", backend="none", ok=False)


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


def directory_bytes(environ: cabc.Mapping[str, str]) -> SizeProbe:
    """Return the local compiler directory's size and how it was obtained.

    The directory is the archive the direct backend would replace, so its size
    is the transfer the migration removes. ``detail`` is ``measured``,
    ``absent``, or ``unreadable``; the last two differ only in the diagnostic,
    and both leave the size unknown.

    Returns
    -------
    SizeProbe
        The summed file sizes, or ``None`` with the reason it is missing.
    """
    root = Path(environ.get("HOME", "")) / ".cache" / "sccache"
    try:
        info = root.stat()
    except FileNotFoundError:
        return SizeProbe(None, "absent")
    except OSError:
        return SizeProbe(None, "unreadable")
    if not stat.S_ISDIR(info.st_mode):
        return SizeProbe(None, "absent")
    try:
        total = _tree_bytes(root)
    except OSError:
        return SizeProbe(None, "unreadable")
    return SizeProbe(total, "measured")


def _tree_bytes(root: Path) -> int:
    """Return the summed file sizes below ``root``, refusing a partial walk.

    ``Path.rglob`` and ``Path.is_file`` swallow ``OSError``, which would report
    an unreadable subtree as a smaller measured total. This traversal lets the
    error propagate so the caller can report the measurement as unknown.

    Returns
    -------
    int
        The summed file sizes; an unusable entry raises ``OSError``.
    """
    total = 0
    with os.scandir(root) as entries:
        for entry in entries:
            # One stat classifies and measures the entry; a vanished or
            # unreadable entry raises and the whole measurement is unknown.
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                total += _tree_bytes(Path(entry.path))
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def rustc(
    environ: cabc.Mapping[str, str], *, runner: Runner = subprocess.run
) -> RustcIdentity:
    """Return the compiler's identity through ``runner``, or ``unknown`` fields.

    Parameters
    ----------
    environ : collections.abc.Mapping[str, str]
        The step environment; ``RUSTC_BINARY`` overrides the binary.
    runner : Runner
        The process runner, defaulting to ``subprocess.run``.

    Returns
    -------
    RustcIdentity
        The bounded release, commit, and host, and whether they were read.
    """
    binary = environ.get("RUSTC_BINARY", "rustc")
    values = {"release": "unknown", "commit": "unknown", "host": "unknown"}
    try:
        completed = runner(
            [binary, "-vV"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return RustcIdentity(
            release="unknown", commit="unknown", host="unknown", ok=False
        )
    if completed.returncode != 0:
        return RustcIdentity(
            release="unknown", commit="unknown", host="unknown", ok=False
        )
    for line in completed.stdout.splitlines():
        name, _, value = line.partition(": ")
        if name == "commit-hash":
            name = "commit"
        if name in values:
            # The compiler's own output is still untrusted text; bound it like
            # every other free-form record field rather than persisting it raw.
            values[name] = schema.bounded(value)
    return RustcIdentity(
        release=values["release"],
        commit=values["commit"],
        host=values["host"],
        ok=True,
    )


def mark_path(environ: cabc.Mapping[str, str], marker: str) -> Path:
    """Return the path a timing marker is stored at."""
    return Path(environ["EVIDENCE_DIR"]) / "marks" / f"{marker}-start"


def _marker_file(environ: cabc.Mapping[str, str], name: str) -> Path:
    """Return the stored file for a marker name, without a suffix."""
    return Path(environ["EVIDENCE_DIR"]) / "marks" / name


def elapsed_between(environ: cabc.Mapping[str, str], start: str, end: str) -> SpanProbe:
    """Return the interval between two markers and how it was obtained.

    Returns
    -------
    SpanProbe
        The non-negative interval, or ``None`` with ``absent`` when a marker
        is missing and ``unreadable`` when it exists but cannot be used.
    """
    begin_path = mark_path(environ, start)
    finish_path = _marker_file(environ, end)
    try:
        begin = float(begin_path.read_text(encoding="utf-8").strip())
        finish = float(finish_path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return SpanProbe(None, "absent")
    except (OSError, ValueError):
        return SpanProbe(None, "unreadable")
    elapsed = finish - begin
    if elapsed < 0:
        return SpanProbe(None, "unreadable")
    return SpanProbe(round(elapsed, 3), "measured")


def write_mark(
    environ: cabc.Mapping[str, str],
    marker: str,
    *,
    clock: cabc.Callable[[], float] = time.time,
) -> bool:
    """Write one wall-clock marker, timed by ``clock``.

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
        path.write_text(f"{clock():.6f}\n", encoding="utf-8")
    except OSError:
        return False
    return True
