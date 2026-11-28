"""Contracts for the local probes the evidence writer performs.

The writer trusts four things it observes directly: sccache's statistics and
the top-level store it reports, the compiler's identity, the local directory's
size, and the two marker files that time a restore or save. Each probe fails
open, and each must reduce what it sees to the bounded value the record
contract accepts.
"""

from __future__ import annotations

import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - CompletedProcess stand-in only.
import typing as typ
from pathlib import Path

from scripts import compiler_cache_evidence as writer
from scripts import compiler_cache_jobs as jobs_reader
from scripts import compiler_cache_probe as probe
from scripts import compiler_cache_schema as schema
from tests.helpers.ci_compiler_evidence import environment

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    """Return a completed process carrying ``stdout``."""
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_the_top_level_cache_location_is_classified_not_returned() -> None:
    """The store is reported beside ``stats``, not inside it."""
    document = (
        '{"stats": {"compile_requests": 1},'
        ' "cache_location": "GitHub Actions Cache", "version": "0.12.0"}'
    )
    observed = probe.read_stats({}, runner=lambda *_, **__: _completed(document))
    assert observed.ok, "a successful read must say so"
    assert observed.stats["compile_requests"] == 1, (
        f"the statistics must be parsed, got {observed.stats}"
    )
    assert observed.version == "0.12.0", (
        f"the version must be preserved, got {observed.version}"
    )
    assert observed.backend == "gha", (
        f"the top-level store must classify as gha, got {observed.backend}"
    )
    assert "cache_location" not in observed.stats, (
        "the raw location must not reach the caller"
    )


def test_a_local_store_classifies_as_local() -> None:
    """A runner path is reduced to a label."""
    document = (
        '{"stats": {},'
        ' "cache_location": "Local disk: \\"/home/runner/.cache/sccache\\"",'
        ' "version": "0.12.0"}'
    )
    observed = probe.read_stats({}, runner=lambda *_, **__: _completed(document))
    assert observed.backend == "local", (
        f"a local directory must classify as local, got {observed.backend}"
    )


def test_cache_counts_are_not_doubled() -> None:
    """The cache increments both maps; its own ``all()`` reads ``counts``."""
    mapping = {"counts": {"Rust": 3}, "adv_counts": {"Rust": 3}}
    assert probe.count(mapping) == 3, "each event must be counted once"


def test_a_newer_sccache_map_shape_is_none() -> None:
    """A document without per-language counts is unknown, never zero."""
    assert probe.count({"unexpected": {}}) is None, (
        "a document with no counts map must be unknown"
    )


def test_the_compiler_commit_field_is_mapped() -> None:
    """``rustc -vV`` names the field ``commit-hash``."""
    version = "release: 1.87.0\ncommit-hash: abc123\nhost: x86_64-linux\n"
    values = probe.rustc({}, runner=lambda *_, **__: _completed(version))
    assert values.ok, "a successful identity read must say so"
    assert values.release == "1.87.0", f"the release must be read, got {values}"
    assert values.commit == "abc123", (
        f"the commit hash must reach the commit field, got {values}"
    )
    assert values.host == "x86_64-linux", f"the host must be read, got {values}"


def test_compiler_identity_text_is_bounded() -> None:
    """A hostile compiler banner must not reach the artefact raw."""
    version = "release: 1.87.0 extra words\ncommit-hash: abc123\nhost: unknown\n"
    values = probe.rustc({}, runner=lambda *_, **__: _completed(version))
    assert values.release == "unknown", (
        f"unbounded compiler text must be dropped, got {values}"
    )
    assert values.commit == "abc123", "a bounded value must be preserved"


def test_an_unset_value_is_bounded_to_unknown() -> None:
    """An empty environment value is missing, not a blank label."""
    assert schema.bounded("") == "unknown", (
        "an empty value must reach the record as unknown, not as a blank"
    )


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


def test_a_job_without_an_attempt_does_not_join() -> None:
    """Paid seconds and the critical path must agree about a missing attempt."""
    record = {"run_id": "1", "run_attempt": "2", "job": "x", "matrix_leg": "none"}
    jobs = [{"run_id": 1, "name": "Rust lint", "conclusion": "success"}]
    assert jobs_reader.matching_job(record, jobs, {"x": "Rust lint"}) is None, (
        "a job with no attempt must not join an attempt-2 record"
    )


def test_a_record_with_no_declared_mapping_is_unknown() -> None:
    """An undeclared job key contributes no whole-job seconds."""
    record = {"run_id": "1", "run_attempt": "1", "job": "x", "matrix_leg": "none"}
    assert jobs_reader.matching_job(record, [], {}) is None, (
        "an undeclared mapping must resolve to no job"
    )


def test_the_marker_files_are_read_verbatim(tmp_path: Path) -> None:
    """The end markers carry no ``-start`` suffix."""
    environ: cabc.Mapping[str, str] = {"EVIDENCE_DIR": str(tmp_path)}
    assert probe.write_mark(environ, "restore"), "the restore start must persist"
    assert probe.write_mark(environ, "restore-end"), "the restore end must persist"
    assert probe.write_mark(environ, "save"), "the save start must persist"
    assert probe.write_mark(environ, "save-end"), "the save end must persist"
    for name in ("restore-start", "restore-end", "save-start", "save-end"):
        assert (tmp_path / "marks" / name).is_file(), f"{name} must be written verbatim"


def test_a_backend_label_outside_the_vocabulary_is_never_ok() -> None:
    """A record cannot be a healthy control without an identified store."""
    assert schema.backend_of("something else") == "unknown", (
        "an unclassifiable store must be unknown, never local"
    )


def test_the_ghac_scheme_classifies_as_the_actions_backend() -> None:
    """The GHA store arrives through OpenDAL's `ghac` scheme."""
    location = "ghac, name: actions-cache, prefix: repo/"
    assert schema.backend_of(location) == "gha", (
        f"a ghac location must classify as gha, got {schema.backend_of(location)!r}"
    )


def _sccache_files(home: Path, sizes: tuple[int, ...]) -> None:
    """Write one cache file of each requested size under ``home``."""
    root = home / ".cache" / "sccache"
    root.mkdir(parents=True)
    for index, size in enumerate(sizes):
        (root / f"object-{index}").write_bytes(b"x" * size)


def test_the_directory_size_is_the_exact_sum(tmp_path: Path) -> None:
    """The archive size is the sum of the files, not an estimate."""
    _sccache_files(tmp_path, (3, 5, 7))
    observed = probe.directory_bytes({"HOME": str(tmp_path)})
    assert observed.size == 15, "the summed sizes must be reported exactly"
    assert observed.detail == "measured", f"the measurement must say so, {observed}"


def test_an_absent_cache_directory_has_no_size(tmp_path: Path) -> None:
    """A directory that does not exist is unknown, never zero."""
    observed = probe.directory_bytes({"HOME": str(tmp_path)})
    assert observed.size is None, "an absent directory must be unknown"
    assert observed.detail == "absent", f"the absence must be explicit, {observed}"


def test_the_directory_size_reaches_the_record(tmp_path: Path) -> None:
    """The probe value must be persisted as the snapshot's byte count."""
    _sccache_files(tmp_path, (11, 13))
    environ = environment(tmp_path)
    state = writer.capture(environ, runner=lambda *_, **__: _completed("{}"))
    assert state["restore_bytes"] == 24, (
        f"the snapshot must carry the measured size, got {state['restore_bytes']}"
    )


def _markers(tmp_path: Path, start: str, end: str) -> dict[str, str]:
    """Write the restore markers and return the probe environment."""
    marks = tmp_path / "marks"
    marks.mkdir()
    (marks / "restore-start").write_text(f"{start}\n", encoding="utf-8")
    (marks / "restore-end").write_text(f"{end}\n", encoding="utf-8")
    return {"EVIDENCE_DIR": str(tmp_path)}


def test_a_missing_marker_is_absent(tmp_path: Path) -> None:
    """No marker at all is a distinct state from a broken one."""
    marks = tmp_path / "marks"
    marks.mkdir()
    (marks / "restore-start").write_text("1.0\n", encoding="utf-8")
    observed = probe.elapsed_between(
        {"EVIDENCE_DIR": str(tmp_path)}, "restore", "restore-end"
    )
    assert observed == probe.SpanProbe(None, "absent"), observed


def test_a_negative_span_is_unreadable(tmp_path: Path) -> None:
    """A start after its end describes a broken timer, not a transfer."""
    observed = probe.elapsed_between(
        _markers(tmp_path, "20.0", "10.0"), "restore", "restore-end"
    )
    assert observed == probe.SpanProbe(None, "unreadable"), observed


def test_a_non_numeric_marker_is_unreadable(tmp_path: Path) -> None:
    """A marker that is not a number cannot time a transfer."""
    observed = probe.elapsed_between(
        _markers(tmp_path, "start", "10.0"), "restore", "restore-end"
    )
    assert observed == probe.SpanProbe(None, "unreadable"), observed


def test_a_failed_marker_write_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write failure must be false, never a silent success."""

    def refuse(*_args: object, **_kwargs: object) -> int:
        """Fail the marker write as a full disk would."""
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(Path, "write_text", refuse)
    observed = probe.write_mark({"EVIDENCE_DIR": str(tmp_path)}, "restore")
    assert observed is False, "a failed write must not claim success"


def test_an_unreadable_cache_directory_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that exists but cannot be walked is not a measured zero."""
    _sccache_files(tmp_path, (3,))
    real_scandir = os.scandir

    def fail_on_cache(path: object) -> object:
        """Fail when the walk reaches the cache root."""
        if Path(typ.cast("str", path)).name == "sccache":
            message = "permission denied"
            raise OSError(message)
        return real_scandir(typ.cast("str", path))

    monkeypatch.setattr(os, "scandir", fail_on_cache)
    observed = probe.directory_bytes({"HOME": str(tmp_path)})
    assert observed.size is None, "an unreadable directory must be unknown"
    assert observed.detail == "unreadable", observed


def test_two_failed_reads_do_not_share_a_stats_mapping() -> None:
    """A caller mutating one result must not change the next read's result."""
    document = '{"version": "0.12.0", "cache_location": "something"}'
    first = probe.read_stats({}, runner=lambda *_, **__: _completed(document))
    second = probe.read_stats({}, runner=lambda *_, **__: _completed(document))
    assert not first.ok, "a missing stats map must fail the first read"
    assert not second.ok, "a missing stats map must fail the second read"
    first.stats["mutated"] = True
    assert "mutated" not in second.stats, "the results must not share state"


def test_a_partial_walk_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unreadable subtree must not become a smaller measured total."""
    root = tmp_path / ".cache" / "sccache"
    (root / "sub").mkdir(parents=True)
    (root / "readable").write_bytes(b"xxx")
    (root / "sub" / "hidden").write_bytes(b"yyyy")
    real_scandir = os.scandir

    def fail_nested(path: object) -> object:
        """Fail when the walk reaches the nested subtree."""
        if Path(typ.cast("str", path)).name == "sub":
            message = "permission denied"
            raise OSError(message)
        return real_scandir(typ.cast("str", path))

    monkeypatch.setattr(os, "scandir", fail_nested)
    observed = probe.directory_bytes({"HOME": str(tmp_path)})
    assert observed == probe.SizeProbe(None, "unreadable"), observed


def test_a_root_status_failure_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache root that cannot be inspected is unknown, not absent."""
    real_stat = Path.stat

    def fail_on_root(self: Path, *, follow_symlinks: bool = True) -> object:
        """Fail when the root's own status is read."""
        if self.name == "sccache":
            message = "permission denied"
            raise OSError(message)
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", fail_on_root)
    observed = probe.directory_bytes({"HOME": str(tmp_path)})
    assert observed == probe.SizeProbe(None, "unreadable"), observed


class _VanishedEntry:
    """A directory entry whose status read fails as if it had vanished."""

    name = "gone"
    path = "/nonexistent/gone"

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        """Fail the way a removed entry does."""
        raise FileNotFoundError(self.name)


class _VanishedScan:
    """A scandir result holding one vanished entry."""

    def __enter__(self) -> list[_VanishedEntry]:
        """Return the scripted entries."""
        return [_VanishedEntry()]

    def __exit__(self, *_exc: object) -> None:
        """Close the scan."""


def test_a_vanished_entry_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry that disappears mid-walk cannot shrink the measured total."""
    (tmp_path / ".cache" / "sccache").mkdir(parents=True)
    monkeypatch.setattr(os, "scandir", lambda _path: _VanishedScan())
    observed = probe.directory_bytes({"HOME": str(tmp_path)})
    assert observed == probe.SizeProbe(None, "unreadable"), observed
