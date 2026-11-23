"""Contracts for the local probes the evidence writer performs.

The writer trusts four things it observes directly: sccache's statistics and
the top-level store it reports, the compiler's identity, the local directory's
size, and the two marker files that time a restore or save. Each probe fails
open, and each must reduce what it sees to the bounded value the record
contract accepts.
"""

from __future__ import annotations

import subprocess  # ruff: ignore[suspicious-subprocess-import] - CompletedProcess stand-in only.
import typing as typ

from scripts import compiler_cache_jobs as jobs_reader
from scripts import compiler_cache_probe as probe
from scripts import compiler_cache_schema as schema

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    import pytest


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    """Return a completed process carrying ``stdout``."""
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_the_top_level_cache_location_is_classified_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store is reported beside ``stats``, not inside it."""
    document = (
        '{"stats": {"compile_requests": 1},'
        ' "cache_location": "GitHub Actions Cache", "version": "0.12.0"}'
    )
    monkeypatch.setattr(probe.subprocess, "run", lambda *_, **__: _completed(document))
    stats, version, backend = probe.read_stats({})
    assert stats["compile_requests"] == 1, f"the statistics must be parsed, got {stats}"
    assert version == "0.12.0"
    assert backend == "gha", f"the top-level store must classify as gha, got {backend}"
    assert "cache_location" not in stats, "the raw location must not reach the caller"


def test_a_local_store_classifies_as_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runner path is reduced to a label."""
    document = (
        '{"stats": {},'
        ' "cache_location": "Local disk: \\"/home/runner/.cache/sccache\\"",'
        ' "version": "0.12.0"}'
    )
    monkeypatch.setattr(probe.subprocess, "run", lambda *_, **__: _completed(document))
    _, _, backend = probe.read_stats({})
    assert backend == "local", (
        f"a local directory must classify as local, got {backend}"
    )


def test_cache_counts_are_not_doubled() -> None:
    """The cache increments both maps; its own ``all()`` reads ``counts``."""
    mapping = {"counts": {"Rust": 3}, "adv_counts": {"Rust": 3}}
    assert probe.count(mapping) == 3, "each event must be counted once"


def test_a_newer_sccache_map_shape_is_none() -> None:
    """A document without per-language counts is unknown, never zero."""
    assert probe.count({"unexpected": {}}) is None


def test_the_compiler_commit_field_is_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """``rustc -vV`` names the field ``commit-hash``."""
    version = "release: 1.87.0\ncommit-hash: abc123\nhost: x86_64-linux\n"
    monkeypatch.setattr(probe.subprocess, "run", lambda *_, **__: _completed(version))
    values = probe.rustc({})
    assert values == {
        "release": "1.87.0",
        "commit": "abc123",
        "host": "x86_64-linux",
    }, f"the commit hash must reach the commit field, got {values}"


def test_an_ambiguous_job_mapping_is_unknown() -> None:
    """A shared substring must not charge an arbitrary job's seconds."""
    record = {"run_id": "1", "run_attempt": "1", "job": "x", "matrix_leg": "none"}
    jobs = [
        {"run_id": 1, "run_attempt": 1, "name": "Rust lint", "conclusion": "success"},
        {"run_id": 1, "run_attempt": 1, "name": "Rust tests", "conclusion": "success"},
    ]
    assert jobs_reader.matching_job(record, jobs, {"x": "Rust"}) is None, (
        "an ambiguous substring must resolve to no job"
    )
    resolved = jobs_reader.matching_job(record, jobs, {"x": "Rust lint"})
    assert resolved is not None, "a unique substring must still resolve"


def test_a_record_with_no_declared_mapping_is_unknown() -> None:
    """An undeclared job key contributes no whole-job seconds."""
    record = {"run_id": "1", "run_attempt": "1", "job": "x", "matrix_leg": "none"}
    assert jobs_reader.matching_job(record, [], {}) is None


def test_the_marker_files_are_read_verbatim(tmp_path: Path) -> None:
    """The end markers carry no ``-start`` suffix."""
    environ: cabc.Mapping[str, str] = {"EVIDENCE_DIR": str(tmp_path)}
    assert probe.write_mark(environ, "restore")
    assert probe.write_mark(environ, "restore-end")
    assert probe.write_mark(environ, "save")
    assert probe.write_mark(environ, "save-end")
    for name in ("restore-start", "restore-end", "save-start", "save-end"):
        assert (tmp_path / "marks" / name).is_file(), f"{name} must be written verbatim"


def test_a_backend_label_outside_the_vocabulary_is_never_ok() -> None:
    """A record cannot be a healthy control without an identified store."""
    assert schema.backend_of("something else") == "unknown", (
        "an unclassifiable store must be unknown, never local"
    )
