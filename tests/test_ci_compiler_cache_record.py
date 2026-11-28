"""Contracts for the pure record assembly and the command edge's clocks.

The writer's reads happen in `gather_observation`; `build_record` receives an
`ObservationBundle` and must not probe anything. These tests prove that
separation directly, then pin each command's clock through `snapshot`,
`record`, and `mark`.
"""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

import pytest

from scripts import compiler_cache_evidence as writer
from scripts import compiler_cache_probe as probe
from scripts import compiler_cache_schema as schema
from scripts import compiler_cache_validation as validation
from scripts.compiler_cache_record import ObservationBundle, build_record

if typ.TYPE_CHECKING:
    import os

from tests.helpers.ci_compiler_evidence import (
    FakeSccache,
    environment,
    record_fixture,
    stats_document,
)

OBSERVED_KEYS = frozenset({
    "restore_seconds",
    "restore_bytes",
    "save_seconds",
    "save_bytes",
    "counters",
    "durations",
    "not_cached",
    "zero_requests",
    "state",
})


def _bundle() -> ObservationBundle:
    """Return a fixed bundle that needs no probe or file."""
    fixture = record_fixture()
    counters = dict.fromkeys(schema.COUNTERS, 0)
    counters["compile_requests"] = 5
    counters["cache_hits"] = 1
    return ObservationBundle(
        fields={
            key: value for key, value in fixture.items() if key not in OBSERVED_KEYS
        },
        pre={
            "counters": dict.fromkeys(schema.COUNTERS, 0),
            "durations": dict.fromkeys(schema.DURATIONS, 0.0),
            "not_cached": {},
            "restore_seconds": 1.0,
            "restore_bytes": 10,
        },
        post={
            "counters": counters,
            "durations": {
                **dict.fromkeys(schema.DURATIONS, 0.0),
                "cache_write_duration": 1.5,
            },
            "not_cached": {},
        },
        save_seconds=3.0,
        save_bytes=4,
        expects_rustc=True,
        target_present=False,
    )


def _probe_forbidden(*_args: object, **_kwargs: object) -> typ.NoReturn:
    """Fail if the pure assembly reaches the probe adapter."""
    message = "the pure assembly must not probe"
    raise AssertionError(message)


def test_the_pure_assembly_never_calls_a_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle in, a validated record out, with every probe forbidden."""
    monkeypatch.setattr(probe, "read_stats", _probe_forbidden)
    monkeypatch.setattr(probe, "rustc", _probe_forbidden)
    monkeypatch.setattr(probe, "directory_bytes", _probe_forbidden)
    monkeypatch.setattr(probe, "elapsed_between", _probe_forbidden)
    record = validation.validate_record(build_record(_bundle()))
    assert record["state"] == "ok", f"a sound fixture must be ok, got {record}"
    assert record["save_seconds"] == pytest.approx(3.0), record
    assert record["restore_bytes"] == 10, record
    assert record["zero_requests"] == "nonzero", record


def test_snapshot_pins_its_clock(tmp_path: Path) -> None:
    """The snapshot command takes the caller's clock."""
    environ = environment(tmp_path)
    status = writer.snapshot(
        environ,
        clock=lambda: "2026-01-01T00:00:00Z",
        runner=FakeSccache([stats_document()]),
    )
    assert status.outcome == "persisted", "the snapshot must be persisted"
    stored = json.loads(
        (tmp_path / "evidence" / "snapshots" / "typecheck-pre.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["captured_at"] == "2026-01-01T00:00:00Z", stored


def test_record_pins_its_clock(tmp_path: Path) -> None:
    """The record command takes the caller's clock."""
    environ = environment(tmp_path)
    for phase in ("pre", "post"):
        writer.snapshot(
            {**environ, "EVIDENCE_PHASE": phase},
            runner=FakeSccache([stats_document()]),
        )
    status = writer.record(
        environ,
        clock=lambda: "2026-01-01T00:00:00Z",
        runner=FakeSccache([stats_document()]),
    )
    assert status.outcome == "persisted", "the record must be persisted"
    line = (tmp_path / "evidence" / "records.jsonl").read_text(encoding="utf-8")
    assert json.loads(line)["recorded_at"] == "2026-01-01T00:00:00Z", line


def test_mark_pins_its_wall_clock(tmp_path: Path) -> None:
    """The mark command takes the caller's wall clock."""
    environ = {**environment(tmp_path), "EVIDENCE_MARKER": "restore"}
    observed = writer.mark(environ, clock=lambda: 5.0)
    assert observed.outcome == "persisted", "the mark must be persisted"
    observed = (tmp_path / "evidence" / "marks" / "restore-start").read_text(
        encoding="utf-8"
    )
    assert observed.strip() == "5.000000", observed


def test_an_unknown_mode_prints_usage_and_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown mode must not silently run a default command."""
    status = writer.main(["compiler_cache_evidence.py", "bogus"], {})
    assert status == 2, f"an unknown mode must fail, got {status}"
    assert "snapshot|record|mark" in capsys.readouterr().err, (
        "the usage line must name the modes"
    )


def _fail_scan(path: Path) -> list[os.DirEntry[str]]:
    """Fail the scan of the cache root, as an unreadable directory does."""
    if path.name == "sccache":
        message = "permission denied"
        raise OSError(message)
    return probe.directory_scan(path)


def _round(
    environ: dict[str, str],
    bracket: str,
    *,
    scan: probe.Scan = probe.directory_scan,
) -> None:
    """Snapshot and record one bracket through ``scan``."""
    for phase in ("pre", "post"):
        writer.snapshot(
            {**environ, "EVIDENCE_BRACKET": bracket, "EVIDENCE_PHASE": phase},
            runner=FakeSccache([stats_document()]),
            scan=scan,
        )
    observed = writer.record(
        {**environ, "EVIDENCE_BRACKET": bracket},
        runner=FakeSccache([stats_document()]),
        scan=scan,
    )
    assert observed.outcome == "persisted", observed


def test_a_readable_cache_writes_integers_and_an_unreadable_one_nulls(
    tmp_path: Path,
) -> None:
    """A measured size is an integer; an unreadable size is null, never zero."""
    cache = tmp_path / ".cache" / "sccache"
    cache.mkdir(parents=True)
    (cache / "object").write_bytes(b"12345")
    environ = environment(tmp_path)
    _round(environ, "typecheck")
    _round(environ, "extension", scan=_fail_scan)
    lines = [
        json.loads(line)
        for line in (tmp_path / "evidence" / "records.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    measured = next(line for line in lines if line["bracket"] == "typecheck")
    unknown = next(line for line in lines if line["bracket"] == "extension")
    assert measured["restore_bytes"] == 5, measured
    assert measured["save_bytes"] == 5, measured
    assert unknown["restore_bytes"] is None, unknown
    assert unknown["save_bytes"] is None, unknown


def _refuse_write(*_args: object, **_kwargs: object) -> int:
    """Fail a write as a read-only directory would."""
    message = "read-only"
    raise OSError(message)


@pytest.mark.parametrize(
    ("command", "fields"),
    [
        ("mark", {"EVIDENCE_MARKER": "restore"}),
        ("snapshot", {}),
    ],
    ids=["marker", "snapshot"],
)
def test_an_unwritable_write_is_an_unwritable_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    fields: dict[str, str],
) -> None:
    """A refused write is visible in the result type for both commands."""
    monkeypatch.setattr(Path, "write_text", _refuse_write)
    environ = {**environment(tmp_path), **fields}
    observed = (
        writer.mark(environ, clock=lambda: 1.0)
        if command == "mark"
        else writer.snapshot(environ, runner=FakeSccache([stats_document()]))
    )
    assert observed.outcome == "unwritable", observed


def test_an_invalid_marker_is_an_omitted_result(tmp_path: Path) -> None:
    """The marker command reports its outcome by type, not stdout."""
    environ = {**environment(tmp_path), "EVIDENCE_MARKER": "bogus"}
    observed = writer.mark(environ)
    assert observed.outcome == "omitted", observed
    assert "Invalid timing marker" in observed.warning, observed


def test_an_invalid_snapshot_is_an_omitted_result(tmp_path: Path) -> None:
    """An unusable bracket is reported in the result type."""
    environ = {**environment(tmp_path), "EVIDENCE_BRACKET": "Bad Bracket!"}
    observed = writer.snapshot(environ)
    assert observed.outcome == "omitted", observed
    assert "Invalid snapshot fields" in observed.warning, observed


def test_an_unwritable_snapshot_is_an_unwritable_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused snapshot write is reported in the result type."""

    def refuse(*_args: object, **_kwargs: object) -> int:
        """Fail the write as a read-only directory would."""
        message = "read-only"
        raise OSError(message)

    monkeypatch.setattr(Path, "write_text", refuse)
    environ = environment(tmp_path)
    observed = writer.snapshot(environ, runner=FakeSccache([stats_document()]))
    assert observed.outcome == "unwritable", observed


def test_an_invalid_record_is_an_omitted_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid record is reported in the result type."""
    monkeypatch.setattr(writer, "build_record", lambda _bundle: {"bracket": "x"})
    observed = writer.record(environment(tmp_path))
    assert observed.outcome == "omitted", observed
