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
from scripts import compiler_cache_probe as probe
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


def _capture(
    environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    """Capture one snapshot with sccache answering the standard document."""
    monkeypatch.setattr(probe.subprocess, "run", FakeSccache([stats_document()]))
    return writer.capture(environ)


def test_a_snapshot_copies_only_allowlisted_counters(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drop everything sccache reports that is not on the list."""
    state = _capture(evidence_environment, monkeypatch)
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
    state = _capture(environ, monkeypatch)
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
    state = _capture(evidence_environment, monkeypatch)
    assert "/home/runner" not in json.dumps(state), "the cache path must not leak"
    assert state["cache_backend"] == "local", (
        f"a local directory must classify as local, got {state['cache_backend']}"
    )


def test_an_absent_server_is_unknown_rather_than_zero(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job that fell back must not report a clean zero-request cache."""
    monkeypatch.setattr(probe.subprocess, "run", FakeSccache([], returncode=127))
    state = writer.capture(evidence_environment)
    assert state["available"] is False, "a failed probe must report unavailable"
    counters = typ.cast("dict[str, object]", state["counters"])
    assert set(counters.values()) == {None}, (
        f"unavailable counters must be null, never zero, got {counters}"
    )
