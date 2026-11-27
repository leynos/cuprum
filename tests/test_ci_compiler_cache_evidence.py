"""Contracts for the compiler-cache evidence writer, reader, and wiring.

The experiment in `docs/ci-cache-ownership.md` adopts a family only when
ordinary runs show a useful whole-job saving. That claim rests on three things
this module holds: the writer records the right numbers and never leaks a
credential, the reader refuses anything that is not the declared schema, and
the workflow brackets each compiling step without being able to fail a build
that compiled correctly.
"""

from __future__ import annotations

import json
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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

HYPOTHESIS = settings(
    deadline=None,
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


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


def _two_snapshots(
    evidence_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    documents: tuple[dict[str, object], dict[str, object]],
) -> None:
    """Write a bracket's pre and post snapshots from two documents."""
    for phase, document in zip(("pre", "post"), documents, strict=True):
        monkeypatch.setattr(probe.subprocess, "run", FakeSccache([document]))
        observed = writer.snapshot({**evidence_environment, "EVIDENCE_PHASE": phase})
        assert observed == 0, f"the {phase} snapshot must succeed"


def test_a_record_subtracts_the_bracket_and_classifies_the_state(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bracket records the work between its two snapshots."""
    _two_snapshots(
        evidence_environment,
        monkeypatch,
        (
            stats_document(),
            stats_document(compile_requests=18, cache_hits={"counts": {"Rust": 11}}),
        ),
    )
    record = writer.build_record(evidence_environment)
    counters = typ.cast("dict[str, object]", record["counters"])
    assert counters["compile_requests"] == 8, (
        f"the record must subtract the pre bracket, got {counters['compile_requests']}"
    )
    assert counters["cache_hits"] == 7, (
        f"the record must subtract the pre bracket, got {counters['cache_hits']}"
    )
    assert record["state"] == "ok", (
        f"a healthy bracket must be ok, got {record['state']}"
    )
    assert record["zero_requests"] == "nonzero", "a compiling bracket is nonzero"


@pytest.mark.parametrize(
    ("expects", "present", "requests", "expected"),
    [
        (True, False, 0, "unexplained"),
        (True, True, 0, "fresh-target"),
        (False, False, 0, "no-rustc-route"),
        (True, False, 3, "nonzero"),
        (None, None, None, "unexplained"),
    ],
)
def test_zero_request_classification(
    expects: bool | None, present: bool | None, requests: int | None, expected: str
) -> None:
    """Separate an honest zero from a hole in the measurement."""
    observed = schema.zero_requests_kind(
        requests, expects_rustc=expects, target_present=present
    )
    assert observed == expected, (
        f"requests={requests}, expects_rustc={expects}, target_present={present} "
        f"must classify as {expected!r}, got {observed!r}"
    )


def _record_after(
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    before: dict[str, object],
    after: dict[str, object],
) -> dict[str, object]:
    """Build one record after two scripted sccache snapshots."""
    _two_snapshots(environ, monkeypatch, (before, after))
    return writer.build_record(environ)


class _ReasonCase(typ.NamedTuple):
    """One scripted pair of reason maps and the deltas it must produce."""

    before: dict[str, int]
    after: dict[str, int]
    expected: dict[str, int]


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _ReasonCase({}, {"unsupported": 3}, {"unsupported": 3}),
            id="new-reason",
        ),
        pytest.param(
            _ReasonCase(
                {"unsupported": 5, "crate_type": 1},
                {"unsupported": 1, "crate_type": 4},
                {"crate_type": 3},
            ),
            id="reset-reason",
        ),
    ],
)
def test_a_reason_delta_is_zero_based_and_reset_aware(
    evidence_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    case: _ReasonCase,
) -> None:
    """An absent reason is zero; a reason that fell is omitted, never negative."""
    record = _record_after(
        evidence_environment,
        monkeypatch,
        stats_document(not_cached=case.before),
        stats_document(not_cached=case.after),
    )
    assert record["not_cached"] == case.expected, (
        f"the reason deltas must be {case.expected!r}, got {record['not_cached']}"
    )


def test_the_record_carries_the_runner_class(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pair must never compare two runner classes as one backend change."""
    record = _record_after(
        evidence_environment, monkeypatch, stats_document(), stats_document()
    )
    assert record["runner_class"] == "self-hosted", (
        f"the runner class must reach the record, got {record}"
    )


def test_the_save_span_ends_at_its_end_marker(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two records after one save must report the same save interval."""
    import time

    marks = tmp = (
        __import__("pathlib").Path(evidence_environment["EVIDENCE_DIR"]) / "marks"
    )
    tmp.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (marks / "save-start").write_text(f"{now - 8:.6f}\n", encoding="utf-8")
    (marks / "save-end").write_text(f"{now - 3:.6f}\n", encoding="utf-8")
    record = _record_after(
        evidence_environment, monkeypatch, stats_document(), stats_document()
    )
    assert record["save_seconds"] == pytest.approx(5.0, abs=1.0), (
        f"the save span must be the interval between the markers, got {record}"
    )


def test_an_unidentified_backend_is_degraded(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store the reader cannot name must not become a healthy control."""
    document = {
        "stats": stats_document()["stats"],
        "cache_location": "something else",
        "version": "0.12.0",
    }
    _two_snapshots(evidence_environment, monkeypatch, (document, document))
    record = writer.build_record(evidence_environment)
    assert record["backend"] == "unknown", record["backend"]
    assert record["state"] == "degraded", (
        f"an unidentified backend must be degraded, got {record['state']}"
    )


def test_a_fallback_record_is_degraded_not_ok(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compiler-only fallback must never count as remote-cache success."""
    _two_snapshots(
        evidence_environment, monkeypatch, (stats_document(), stats_document())
    )
    record = writer.build_record({
        **evidence_environment,
        "EVIDENCE_SETUP_STATUS": "fallback",
    })
    assert record["state"] == "degraded", (
        f"a fallback must be degraded, got {record['state']}"
    )


def test_a_gha_backend_on_the_v2_service_is_noncompliant(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v2 service resolves past the proxy, so it is the wrong store."""
    environ = {
        **evidence_environment,
        "ACTIONS_CACHE_SERVICE_V2": "1",
        "ACTIONS_CACHE_URL": BEARER_URL,
    }
    document = stats_document(cache_location="GitHub Actions Cache")
    _two_snapshots(environ, monkeypatch, (document, document))
    record = writer.build_record({**environ, "EVIDENCE_SELECTED_BACKEND": "gha"})
    assert record["backend"] == "gha", "the reported backend must be recorded"
    assert record["state"] == "noncompliant", (
        f"a v2 service with the gha backend must be noncompliant, got {record['state']}"
    )


def test_an_invalid_bracket_omits_the_record_without_failing(
    evidence_environment: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail open in the job, and never echo the rejected value."""
    observed = writer.record({
        **evidence_environment,
        "EVIDENCE_BRACKET": "Bad Bracket!",
    })
    assert observed == 0, "an invalid bracket must not fail the step"
    captured = capsys.readouterr()
    assert "record omitted" in captured.out, captured.out
    assert "Bad Bracket!" not in captured.out, "a rejected value must not be echoed"


@HYPOTHESIS
@given(
    post=st.integers(min_value=0, max_value=10**6),
    pre=st.integers(min_value=0, max_value=10**6),
)
def test_a_counter_delta_is_arithmetic(post: int, pre: int) -> None:
    """A rising counter subtracts; a falling one is unknown, never negative."""
    expected = post - pre if post >= pre else None
    assert schema.delta_value(post, pre) == expected, (
        f"the delta of {post} and {pre} must be {expected!r}"
    )


def test_a_restarted_server_leaves_the_counter_delta_unknown() -> None:
    """A zeroed statistics document says nothing about this bracket's work."""
    assert schema.delta_value(0, 5) is None, (
        "a counter that fell must be unknown rather than negative"
    )


@HYPOTHESIS
@given(value=st.one_of(st.none(), st.text(), st.booleans()))
def test_an_unknown_counter_delta_stays_unknown(value: object) -> None:
    """Never invent a delta from a value the server did not report."""
    assert schema.delta_value(value, 1) is None, (
        f"a post value of {value!r} must not produce a delta"
    )
    assert schema.delta_value(None, value) is None, (
        f"a pre value of {value!r} must not produce a delta"
    )


def test_the_restore_span_ends_at_its_end_marker(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The span must stop at the restore, not at the later snapshot."""
    import time

    marks = tmp = (
        __import__("pathlib").Path(evidence_environment["EVIDENCE_DIR"]) / "marks"
    )
    tmp.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (marks / "restore-start").write_text(f"{now - 10:.6f}\n", encoding="utf-8")
    (marks / "restore-end").write_text(f"{now - 4:.6f}\n", encoding="utf-8")
    monkeypatch.setattr(probe.subprocess, "run", FakeSccache([stats_document()]))
    state = writer.capture(evidence_environment)
    assert state["restore_seconds"] == pytest.approx(6.0, abs=1.0), (
        f"the restore span must be the interval between the markers, got {state}"
    )


def test_a_selected_backend_that_differs_from_the_server_is_noncompliant(
    evidence_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record must not claim a store the action did not bind."""
    document = stats_document(cache_location="GitHub Actions Cache")
    _two_snapshots(evidence_environment, monkeypatch, (document, document))
    record = writer.build_record({
        **evidence_environment,
        "EVIDENCE_SELECTED_BACKEND": "local",
    })
    assert record["state"] == "noncompliant", (
        f"a local selection against a gha server must be noncompliant, got "
        f"{record['state']}"
    )
