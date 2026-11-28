"""Run the compiler-cache evidence commands and report their outcomes.

Each command returns a typed ``CommandResult``: ``persisted``, ``omitted`` for
invalid fields or a missing evidence directory, or ``unwritable`` when the
output cannot be stored. ``main`` maps that result to exit status 0 and prints
one fixed warning that names the failing stage. Clocks, the process runner,
the directory scan, marker reads, path status, and the two write paths are
injectable, so a test can pin every edge dependency.
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
from scripts.compiler_cache_snapshots import (
    Clock,
    SnapshotEdge,
    capture,
    load_bracket,
    snapshot_path,
)
from scripts.compiler_cache_validation import validate_record

_WARNING = "::warning title=compiler-cache-evidence::"
_MISSING_DIR = f"{_WARNING}Evidence omitted: the evidence directory is not configured."
_SNAPSHOT_INVALID = f"{_WARNING}Snapshot omitted: invalid evidence fields."
_SNAPSHOT_UNWRITABLE = f"{_WARNING}Snapshot omitted: could not persist."
_RECORD_INVALID = f"{_WARNING}Record omitted: invalid evidence fields."
_RECORD_UNWRITABLE = f"{_WARNING}Record omitted: could not persist."
_MARK_INVALID = f"{_WARNING}Mark omitted: invalid timing marker."
_MARK_UNWRITABLE = f"{_WARNING}Mark omitted: could not persist."

type WallClock = cabc.Callable[[], float]
type WriteText = cabc.Callable[[Path, str], None]


class CommandResult(typ.NamedTuple):
    """What one writer command did with its output."""

    outcome: str
    warning: str = ""


def write_text_file(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as UTF-8, creating the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def append_text_file(path: Path, text: str) -> None:
    """Append ``text`` to ``path`` as UTF-8, creating the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


class WriterEdge(typ.NamedTuple):
    """The clocks, runner, scans, and write paths a caller may pin."""

    clock: Clock = schema.now
    wall_clock: WallClock = time.time
    runner: probe.Runner = subprocess.run
    scan: probe.Scan = probe.directory_scan
    read: probe.ReadText = probe.read_text
    stat_path: probe.PathStat = Path.stat
    write: WriteText = write_text_file
    append: WriteText = append_text_file


_DEFAULT_EDGE = WriterEdge()


def _missing_dir(environ: cabc.Mapping[str, str]) -> CommandResult | None:
    """Return the omitted result when the evidence directory is unset."""
    if "EVIDENCE_DIR" in environ:
        return None
    return CommandResult("omitted", _MISSING_DIR)


def write_mark(
    environ: cabc.Mapping[str, str],
    marker: str,
    *,
    clock: WallClock = time.time,
    write: WriteText = write_text_file,
) -> bool:
    """Persist one timing marker, timed by ``clock`` and written by ``write``."""
    path = probe.marker_path(environ, marker)
    try:
        write(path, f"{clock():.6f}\n")
    except OSError:
        return False
    return True


def gather_observation(
    environ: cabc.Mapping[str, str], *, edge: WriterEdge = _DEFAULT_EDGE
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
        If a snapshot is missing or malformed, or the run identity is invalid.
    """  # ruff: ignore[docstring-extraneous-exception] - `load_bracket` and `run_identity` raise ValueError.
    bracket, pre, post = load_bracket(environ)
    identity = run_identity(environ)
    fields = identity_fields(environ, bracket, identity, post)
    compiler = probe.rustc(environ, runner=edge.runner)
    fields["recorded_at"] = edge.clock()
    fields["rustc"] = {
        "release": compiler.release,
        "commit": compiler.commit,
        "host": compiler.host,
    }
    save_span = probe.elapsed_between(environ, "save", "save-end", read=edge.read)
    save_size = probe.directory_bytes(environ, scan=edge.scan, stat_path=edge.stat_path)
    return ObservationBundle(
        fields=fields,
        pre=pre,
        post=post,
        save_seconds=save_span.seconds,
        save_seconds_detail=save_span.detail,
        save_bytes=save_size.size,
        save_bytes_detail=save_size.detail,
        expects_rustc=schema.boolean(environ.get("EVIDENCE_EXPECTS_RUSTC")),
        target_present=schema.boolean(environ.get("EVIDENCE_TARGET_PRESENT")),
    )


def snapshot(
    environ: cabc.Mapping[str, str], *, edge: WriterEdge = _DEFAULT_EDGE
) -> CommandResult:
    """Write one snapshot; never fail the job for a missing cache server.

    Returns
    -------
    CommandResult
        ``persisted``, ``omitted`` for a missing directory or invalid fields,
        or ``unwritable`` when the snapshot cannot be stored.
    """
    missing = _missing_dir(environ)
    if missing is not None:
        return missing
    try:
        state = capture(
            environ,
            edge=SnapshotEdge(
                clock=edge.clock,
                runner=edge.runner,
                scan=edge.scan,
                read=edge.read,
                stat_path=edge.stat_path,
            ),
        )
        path = snapshot_path(environ, str(state["bracket"]), str(state["phase"]))
        edge.write(path, json.dumps(state, separators=(",", ":")) + "\n")
    except ValueError:
        return CommandResult("omitted", _SNAPSHOT_INVALID)
    except OSError:
        return CommandResult("unwritable", _SNAPSHOT_UNWRITABLE)
    return CommandResult("persisted")


def mark(
    environ: cabc.Mapping[str, str], *, edge: WriterEdge = _DEFAULT_EDGE
) -> CommandResult:
    """Write a wall-clock marker before a restore or save; never fail the job.

    Returns
    -------
    CommandResult
        ``persisted``, ``omitted`` for a missing directory or invalid marker,
        or ``unwritable`` when the marker cannot be stored.
    """
    missing = _missing_dir(environ)
    if missing is not None:
        return missing
    marker = environ.get("EVIDENCE_MARKER", "")
    if marker not in {"restore", "restore-end", "save", "save-end"}:
        return CommandResult("omitted", _MARK_INVALID)
    if not write_mark(environ, marker, clock=edge.wall_clock, write=edge.write):
        return CommandResult("unwritable", _MARK_UNWRITABLE)
    return CommandResult("persisted")


def record(
    environ: cabc.Mapping[str, str], *, edge: WriterEdge = _DEFAULT_EDGE
) -> CommandResult:
    """Append one record; fail open with a fixed warning on invalid input.

    Returns
    -------
    CommandResult
        ``persisted``, ``omitted`` for a missing directory or invalid fields,
        or ``unwritable`` when the record cannot be stored.
    """
    missing = _missing_dir(environ)
    if missing is not None:
        return missing
    try:
        # One malformed line makes the reader reject the whole artefact.
        document = validate_record(build_record(gather_observation(environ, edge=edge)))
        edge.append(
            Path(environ["EVIDENCE_DIR"]) / "records.jsonl",
            json.dumps(document, separators=(",", ":")) + "\n",
        )
    except ValueError:
        return CommandResult("omitted", _RECORD_INVALID)
    except OSError:
        return CommandResult("unwritable", _RECORD_UNWRITABLE)
    return CommandResult("persisted")


def main(
    argv: list[str],
    environ: cabc.Mapping[str, str],
    *,
    edge: WriterEdge = _DEFAULT_EDGE,
) -> int:
    """Dispatch one writer mode and return its exit status."""
    mode = argv[1] if len(argv) > 1 else ""
    match mode:
        case "snapshot":
            result = snapshot(environ, edge=edge)
        case "record":
            result = record(environ, edge=edge)
        case "mark":
            result = mark(environ, edge=edge)
        case _:
            print(f"usage: {Path(argv[0]).name} snapshot|record|mark", file=sys.stderr)
            return 2
    if result.warning:
        print(result.warning)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
