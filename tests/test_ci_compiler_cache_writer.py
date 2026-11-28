"""Contracts for one compiler-cache snapshot.

A snapshot copies only the allowlisted statistics, reduces the store to a
label, and treats an unreadable server as unavailable rather than as a clean
zero. These are the fields every later delta and classification rests on.
"""

from __future__ import annotations

import json
import typing as typ

import pytest

from scripts import compiler_cache_evidence as writer
from scripts import compiler_cache_schema as schema
from tests.helpers.ci_compiler_evidence import (
    BEARER_URL,
    SENTINEL_VALUE,
    FakeSccache,
    environment,
    stats_document,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(name="evidence_environment")
def evidence_environment_fixture(tmp_path: Path) -> dict[str, str]:
    """Return a minimal bracket environment rooted in a temporary directory."""
    return environment(tmp_path)


def _capture(environ: dict[str, str], runner: FakeSccache) -> dict[str, object]:
    """Capture one snapshot with sccache answering through ``runner``."""
    return writer.capture(environ, edge=writer.SnapshotEdge(runner=runner))


def test_a_snapshot_copies_only_allowlisted_counters(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drop everything sccache reports that is not on the list."""
    state = _capture(evidence_environment, FakeSccache([stats_document()]))
    counters = typ.cast("dict[str, object]", state["counters"])
    assert set(counters) == set(schema.COUNTERS), (
        f"only the allowlisted counters may be copied, got {sorted(counters)}"
    )
    assert counters["cache_hits"] == 4, "per-language hit counts must be summed"
    assert counters["cache_misses"] == 5, "per-language miss counts must be summed"
    durations = typ.cast("dict[str, object]", state["durations"])
    assert durations["cache_write_duration"] == pytest.approx(1.5), (
        f"durations must combine secs and nanos, got {durations}"
    )


def test_a_snapshot_never_records_the_cache_address(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redact the bearer URL and runtime credential, keeping only a host class."""
    environ = {
        **evidence_environment,
        "ACTIONS_CACHE_URL": BEARER_URL,
        "ACTIONS_RUNTIME_TOKEN": SENTINEL_VALUE,
    }
    state = _capture(environ, FakeSccache([stats_document()]))
    encoded = json.dumps(state)
    assert "example.invalid" not in encoded, "the cache URL must not be recorded"
    assert SENTINEL_VALUE not in encoded, "the runtime credential must not be recorded"
    assert state["cache_host_class"] == "non-github", (
        f"a proxy host must classify as non-github, got {state['cache_host_class']}"
    )


def test_the_cache_location_is_classified_not_recorded(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reduce a runner path to a backend label."""
    state = _capture(evidence_environment, FakeSccache([stats_document()]))
    assert "/home/runner" not in json.dumps(state), "the cache path must not leak"
    assert state["cache_backend"] == "local", (
        f"a local directory must classify as local, got {state['cache_backend']}"
    )


def test_capture_uses_the_injected_clock(evidence_environment: dict[str, str]) -> None:
    """The snapshot timestamp is supplied by the caller, not read inside."""
    state = writer.capture(
        evidence_environment,
        edge=writer.SnapshotEdge(clock=lambda: "2026-01-01T00:00:00Z"),
    )
    assert state["captured_at"] == "2026-01-01T00:00:00Z", (
        f"the injected clock must stamp the snapshot, got {state['captured_at']!r}"
    )


def test_write_mark_uses_the_injected_clock(tmp_path: Path) -> None:
    """The marker is timed by the supplied clock, so a test can pin it."""
    environ = {"EVIDENCE_DIR": str(tmp_path)}
    assert writer.write_mark(environ, "restore", clock=lambda: 1000.0), (
        "the marker must be written"
    )
    observed = (tmp_path / "marks" / "restore-start").read_text(encoding="utf-8")
    assert observed.strip() == "1000.000000", (
        f"the clock must time the mark, got {observed!r}"
    )


def test_an_absent_server_is_unknown_rather_than_zero(
    evidence_environment: dict[str, str],
) -> None:
    """A job that fell back must not report a clean zero-request cache."""
    state = writer.capture(
        evidence_environment,
        edge=writer.SnapshotEdge(runner=FakeSccache([], returncode=127)),
    )
    assert state["available"] is False, "a failed probe must report unavailable"
    counters = typ.cast("dict[str, object]", state["counters"])
    assert set(counters.values()) == {None}, (
        f"unavailable counters must be null, never zero, got {counters}"
    )
