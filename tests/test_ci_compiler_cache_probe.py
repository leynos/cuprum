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

from scripts import compiler_cache_evidence as writer
from scripts import compiler_cache_jobs as jobs_reader
from scripts import compiler_cache_probe as probe
from scripts import compiler_cache_schema as schema
from tests.helpers.ci_compiler_evidence import environment

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


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
