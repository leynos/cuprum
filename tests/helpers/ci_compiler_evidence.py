"""Shared documents for the compiler-cache evidence contracts.

The writer tests and the reader tests must agree about what sccache reports and
what a valid record looks like, or the round trip they assert is not the round
trip the workflow performs. Keeping the fixtures here means a change to one is
visible to the other.

The sccache document includes extra fields, a runner path, and a per-language
count map, because the writer's job is to copy only the allowlisted ones and a
document that contained nothing else would not exercise that.
"""

from __future__ import annotations

import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - CompletedProcess stand-in only.
import typing as typ

from scripts import compiler_cache_schema as schema

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

EVIDENCE_ACTION = ".github/actions/compiler-cache-evidence"
EVIDENCE_ACTION_PATH = "./.github/actions/compiler-cache-evidence"
#: The brackets each instrumented job declares, in the order the workflow runs
#: them. Two brackets never overlap: a shared server's counters are deltas.
BRACKETS: typ.Final = {
    ("ci.yml", "lint-test"): ("lint",),
    ("ci.yml", "typecheck-test"): ("typecheck", "python-tests"),
    ("ci.yml", "extension-tests"): ("extension",),
}
#: A URL and a token the writer must never persist. The token's name avoids a
#: lint heuristic that reads any literal assigned to a `*TOKEN` name as a
#: credential; here it is the sentinel the redaction test searches for.
BEARER_URL = "https://cache-proxy.example.invalid/bearer?token=super-secret"
SENTINEL_VALUE = "runtime-secret-value"


DEFAULT_LOCATION = 'Local disk: "/home/runner/.cache/sccache"'


def stats_document(
    cache_location: str = DEFAULT_LOCATION, **overrides: object
) -> dict[str, object]:
    """Return an sccache statistics document with a full counter set.

    ``cache_location`` is a top-level field in sccache's JSON, not a counter;
    ``overrides`` replace individual counters.

    Returns
    -------
    dict[str, object]
        A document shaped like ``sccache --show-stats --stats-format json``.
    """
    stats: dict[str, object] = {
        "compile_requests": 10,
        "requests_executed": 9,
        "requests_unsupported_compiler": 0,
        "requests_not_compile": 1,
        "requests_not_cacheable": 0,
        "cache_hits": {"counts": {"Rust": 4}, "adv_counts": {}},
        "cache_misses": {"counts": {"Rust": 5}, "adv_counts": {}},
        "cache_timeouts": 0,
        "cache_read_errors": 0,
        "cache_write_errors": 0,
        "cache_writes": 5,
        "compile_fails": 0,
        "non_cacheable_compilations": 0,
        "forced_recaches": 0,
        "dist_errors": 0,
        "cache_write_duration": {"secs": 1, "nanos": 500_000_000},
        "cache_read_hit_duration": {"secs": 0, "nanos": 0},
        "compiler_write_duration": {"secs": 2, "nanos": 0},
        "not_cached": {"crate_type": 3},
    }
    stats.update(overrides)
    return {
        "stats": stats,
        "cache_location": cache_location,
        "version": "0.12.0",
    }


class FakeSccache:
    """Answer each sccache invocation from a scripted list of documents.

    Parameters
    ----------
    documents:
        The documents to answer with, in call order. The last one repeats.
    returncode:
        The exit status every call reports. A non-zero status models a missing
        or refused server, which the writer must record as unavailable.
    """

    def __init__(
        self, documents: cabc.Sequence[dict[str, object]], returncode: int = 0
    ) -> None:
        """Bind the scripted documents and the exit status every call reports."""
        self._documents = list(documents)
        self._returncode = returncode
        self._calls = 0

    def __call__(
        self, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        """Return the next scripted document as a completed process."""
        payload = ""
        if self._documents:
            document = self._documents[min(self._calls, len(self._documents) - 1)]
            payload = json.dumps(document)
        self._calls += 1
        return subprocess.CompletedProcess(argv, self._returncode, payload, "")


def environment(tmp_path: Path) -> dict[str, str]:
    """Return a minimal bracket environment rooted in a temporary directory."""
    return {
        "EVIDENCE_DIR": str(tmp_path / "evidence"),
        "EVIDENCE_BRACKET": "typecheck",
        "EVIDENCE_PHASE": "pre",
        "EVIDENCE_FAMILY": "py3.12-debug",
        "EVIDENCE_RUNNER_CLASS": "self-hosted",
        "EVIDENCE_MATRIX_LEG": "3.12",
        "EVIDENCE_COMPILER_SHAPE": "debug",
        "EVIDENCE_PYTHON_VERSION": "3.12",
        "EVIDENCE_TOOLCHAIN": "nightly-2026-08-23",
        "EVIDENCE_DEV_FAST": "true",
        "EVIDENCE_EXPECTS_RUSTC": "true",
        "EVIDENCE_TARGET_PRESENT": "false",
        "EVIDENCE_SETUP_STATUS": "started",
        "EVIDENCE_SELECTED_BACKEND": "local",
        "EVIDENCE_PR_NUMBER": "42",
        "GITHUB_RUN_ID": "123456789",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "typecheck-test",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_WORKFLOW_REF": (
            "leynos/cuprum/.github/workflows/ci.yml@refs/pull/1/merge"
        ),
        "RUSTC_WRAPPER": "/home/runner/.local/bin/sccache",
        "SCCACHE_BINARY": "/nonexistent/sccache",
        "RUSTC_BINARY": "/nonexistent/rustc",
        "HOME": str(tmp_path),
    }


def record_fixture(**overrides: object) -> dict[str, object]:
    """Return a valid record, before validation, with optional overrides."""
    record: dict[str, object] = {
        "schema_version": schema.SCHEMA_VERSION,
        "record_kind": schema.RECORD_KIND,
        "bracket": "typecheck",
        "run_id": "1",
        "run_attempt": "1",
        "job": "typecheck-test",
        "workflow": "ci.yml",
        "event_class": "pull_request",
        "pr_number": "42",
        "recorded_at": "2026-11-22T00:00:00Z",
        "setup_status": "started",
        "backend": "local",
        "selected_backend": "local",
        "wrapper_present": True,
        "cache_service_v2": False,
        "cache_host_class": "absent",
        "sccache_version": "0.12.0",
        "rustc": {"release": "1.87.0", "commit": "unknown", "host": "unknown"},
        "family": "py3.12-debug",
        "runner_class": "self-hosted",
        "matrix_leg": "3.12",
        "compiler_shape": "debug",
        "python_version": "3.12",
        "toolchain": "nightly-2026-08-23",
        "dev_fast": True,
        "checkout_sha": "unknown",
        "head_sha": "unknown",
        "base_sha": "unknown",
        "matched_key": "sccache-1",
        "restore_hit": True,
        "restore_seconds": 1.0,
        "restore_seconds_detail": "measured",
        "restore_bytes": 1024,
        "restore_bytes_detail": "measured",
        "save_seconds": 2.0,
        "save_seconds_detail": "measured",
        "save_bytes": 2048,
        "save_bytes_detail": "measured",
        "counters": dict.fromkeys(schema.COUNTERS, None),
        "durations": dict.fromkeys(schema.DURATIONS, None),
        "not_cached": {},
        "zero_requests": "nonzero",
        "state": "ok",
    }
    record.update(overrides)
    return record
