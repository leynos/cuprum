"""Contracts for the pure record assembly and the command edge's clocks.

The writer's reads happen in `gather_observation`; `build_record` receives an
`ObservationBundle` and must not probe anything. These tests prove that
separation directly, then pin each command's clock through `snapshot`,
`record`, and `mark`.
"""

from __future__ import annotations

import json
import typing as typ

import pytest

from scripts import compiler_cache_evidence as writer
from scripts import compiler_cache_probe as probe
from scripts import compiler_cache_schema as schema
from scripts import compiler_cache_validation as validation
from scripts.compiler_cache_record import ObservationBundle, build_record
from tests.helpers.ci_compiler_evidence import (
    FakeSccache,
    environment,
    record_fixture,
    stats_document,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

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
    assert status == 0, "the snapshot must succeed"
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
    assert status == 0, "the record must succeed"
    line = (tmp_path / "evidence" / "records.jsonl").read_text(encoding="utf-8")
    assert json.loads(line)["recorded_at"] == "2026-01-01T00:00:00Z", line


def test_mark_pins_its_wall_clock(tmp_path: Path) -> None:
    """The mark command takes the caller's wall clock."""
    environ = {**environment(tmp_path), "EVIDENCE_MARKER": "restore"}
    assert writer.mark(environ, clock=lambda: 5.0) == 0, "the mark must succeed"
    observed = (tmp_path / "evidence" / "marks" / "restore-start").read_text(
        encoding="utf-8"
    )
    assert observed.strip() == "5.000000", observed
