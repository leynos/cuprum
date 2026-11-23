"""Measure one pytest invocation and describe it as a durable record.

Issue #607's central question is what a process boundary costs, and a bare wall
clock is not enough to answer it. A run that waited 700 seconds for a compile
slot and a run that compiled for 700 seconds take the same time and mean
opposite things, so every record here carries child CPU time and peak resident
memory beside the wall time, plus the disk headroom the run had and a count of
Cargo's build-lock waits.

The record is deliberately a plain dictionary rather than a dataclass. It is
serialized immediately, read by a separate analysis tool, and versioned by
`schema_version`, so the wire shape is the interface; a dataclass would add a
second definition of that shape without adding a guarantee.

Fields that a given host cannot supply are recorded as `None` with a stated
reason, never omitted and never guessed. `sccache` is the important case: the
statistics only exist when a server is running, and reporting an empty mapping
would read as "the cache did nothing" rather than "the cache was not asked".
"""

from __future__ import annotations

import dataclasses as dc
import json
import os
import pathlib as pth
import resource
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell.
import time
import typing as typ
import uuid

from scripts.atomic_write import write_json

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "SCHEMA_VERSION",
    "Envelope",
    "Invocation",
    "cargo_lock_waits",
    "disk_free_kib",
    "provenance",
    "sccache_statistics",
)

#: Bumped when a field changes meaning, so an old artefact is not silently
#: compared against a new one.
SCHEMA_VERSION = 1

#: Cargo's own wording when a build waits on the package-cache lock. Counting
#: these lines is a lower bound: a wait that produces no line is invisible, so
#: the record calls the count a bound rather than a total.
_CARGO_LOCK_MARKER = b"Blocking waiting for file lock"

#: The provenance keys a workflow passes. Absent values are recorded as
#: `None`, because "this run did not say" and "this run said nothing" must not
#: look the same to the analysis tool.
_PROVENANCE_KEYS = (
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_JOB",
    "GITHUB_WORKFLOW",
    "GITHUB_EVENT_NAME",
    "GITHUB_SHA",
    "GITHUB_BASE_SHA",
    "GITHUB_HEAD_SHA",
    "RUNNER_NAME",
    "RUNNER_OS",
    "CUPRUM_PYTEST_LAYOUT_ARM",
)


@dc.dataclass(slots=True)
class Invocation:
    """One child process, timed and accounted for.

    Parameters
    ----------
    argv : Sequence of str
        The exact argument vector, recorded so a reader can reproduce the run
        without inferring it from a log.
    started : float
        Monotonic start.
    """

    argv: tuple[str, ...]
    started: float
    finished: float | None = None
    exit_status: int | None = None
    signal_number: int | None = None
    incomplete: bool = False
    stdout_path: pth.Path | None = None

    @property
    def seconds(self) -> float:
        """Wall seconds the child occupied, or a partial value if it is running."""
        end = self.finished if self.finished is not None else time.monotonic()
        return round(end - self.started, 3)


def disk_free_kib(path: pth.Path) -> int:
    """Return free space in KiB on the filesystem holding ``path``.

    Parameters
    ----------
    path : pathlib.Path
        A path on the filesystem of interest. An absent path is walked up to
        its nearest existing parent, so a directory created mid-run still
        reports the filesystem it will land on.

    Returns
    -------
    int
        Free space in kibibytes.
    """
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return shutil.disk_usage(candidate).free // 1024


def sccache_statistics() -> dict[str, object] | None:
    """Return the running sccache server's JSON statistics, or ``None``.

    Returns
    -------
    dict or None
        The parsed statistics, or ``None`` when no server is available or the
        tool cannot be run. `None` says the cache was not consulted; an empty
        mapping would wrongly say it was consulted and did nothing.
    """
    try:
        completed = subprocess.run(
            # ruff: ignore[start-process-with-partial-path] - sccache is on PATH.
            ["sccache", "--show-stats", "--stats-format", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def cargo_lock_waits(path: pth.Path | None) -> int:
    """Count Cargo's build-lock wait lines in a captured log.

    Parameters
    ----------
    path : pathlib.Path or None
        Captured combined output, or ``None`` when output was not captured.

    Returns
    -------
    int
        A lower bound on the number of lock waits, since Cargo prints one line
        per wait it begins and none for a wait that resolves instantly.
    """
    if path is None or not path.exists():
        return 0
    return path.read_bytes().count(_CARGO_LOCK_MARKER)


def provenance() -> dict[str, str | None]:
    """Return the CI identity a record needs to be paired with another run.

    Returns
    -------
    dict
        Each known provenance key mapped to its value, or to ``None`` when the
        variable is unset. `None` is preserved rather than dropped, so a local
        run is distinguishable from a CI run that omitted a variable.
    """
    return {key: os.environ.get(key) for key in _PROVENANCE_KEYS}


class Envelope:
    """Measure a whole invocation and build its durable record.

    The envelope samples the surrounding resources - free disk and the compiler
    cache - before and after the child, so a candidate cannot look cheaper by
    filling the disk or by silently losing its cache. Sampling is deliberately
    coarse: one `disk_usage` call and one `sccache --show-stats` per side, which
    is bounded work that cannot itself dominate the measurement.

    Parameters
    ----------
    kind : str
        ``wrap`` for one child process, ``run`` for a sequence of groups.
    evidence_dir : pathlib.Path or None
        Where to persist the record, or ``None`` to keep it in memory only.
    """

    def __init__(self, kind: str, evidence_dir: pth.Path | None) -> None:
        """Take the before-sample and start the clock."""
        self.kind = kind
        self.evidence_dir = evidence_dir
        self.invocation_id = uuid.uuid4().hex
        self.started_wall = time.time()
        self.started_monotonic = time.monotonic()
        self.disk_before = disk_free_kib(pth.Path.cwd())
        self.sccache_before = sccache_statistics()
        self.children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.invocations: list[dict[str, object]] = []
        self.extra: dict[str, object] = {}

    def add(self, invocation: Invocation, *, files: cabc.Sequence[str]) -> None:
        """Record one finished or interrupted child.

        Parameters
        ----------
        invocation : Invocation
            The child, with its timing and status already set.
        files : Sequence of str
            The test files the child selected, in argv order.
        """
        self.invocations.append({
            "argv": list(invocation.argv),
            "files": list(files),
            "seconds": invocation.seconds,
            "exit_status": invocation.exit_status,
            "signal": invocation.signal_number,
            "incomplete": invocation.incomplete,
            "cargo_lock_waits": cargo_lock_waits(invocation.stdout_path),
        })

    def build(self, exit_status: int, *, incomplete: bool = False) -> dict[str, object]:
        """Return the finished record.

        Parameters
        ----------
        exit_status : int
            The status the driver will exit with.
        incomplete : bool
            Whether the invocation was interrupted, so the record is a partial
            observation rather than a finished one.

        Returns
        -------
        dict
            A JSON-serializable record carrying :data:`SCHEMA_VERSION`.
        """
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        record: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind,
            "invocation_id": self.invocation_id,
            "started": self.started_wall,
            "finished": time.time(),
            "wall_seconds": round(time.monotonic() - self.started_monotonic, 3),
            "exit_status": exit_status,
            "incomplete": incomplete,
            "cpu_seconds": round(
                (after.ru_utime - self.children_before.ru_utime)
                + (after.ru_stime - self.children_before.ru_stime),
                3,
            ),
            "max_rss_kib": after.ru_maxrss,
            "disk_free_kib_before": self.disk_before,
            "disk_free_kib_after": disk_free_kib(pth.Path.cwd()),
            "sccache_before": self.sccache_before,
            "sccache_after": sccache_statistics(),
            "invocations": self.invocations,
            "provenance": provenance(),
        }
        record.update(self.extra)
        return record

    def persist(self, record: cabc.Mapping[str, object]) -> pth.Path | None:
        """Write the record when an evidence directory was requested.

        Parameters
        ----------
        record : collections.abc.Mapping of str to object
            The record to write.

        Returns
        -------
        pathlib.Path or None
            The written path, or ``None`` when no directory was requested or
            the write failed. A failed write is reported to standard error and
            swallowed, so a diagnostic cannot fail the run it describes.
        """
        if self.evidence_dir is None:
            return None
        destination = (
            self.evidence_dir / f"pytest-{self.kind}-{self.invocation_id}.json"
        )
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            return write_json(destination, record)
        except OSError as error:
            print(f"pytest batches: could not write {destination}: {error}")
            return None
