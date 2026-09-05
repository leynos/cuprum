"""Unit tests for benchmark-ratchet history JSON and filesystem persistence."""

from __future__ import annotations

import json
import typing as typ

import pytest

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ratchet_history import BaselineHistory, HistorySample
from benchmarks.ratchet_history_persistence import (
    BaselineHistoryNotFoundError,
    BaselineHistoryReadError,
    history_from_payload,
    history_to_payload,
    load_history,
    write_history,
)
from cuprum.unittests.conftest import SCENARIO, TYPICAL_RATIOS, WORKER_ITERATIONS

if typ.TYPE_CHECKING:
    import pathlib as pth


def _sample(ratio: float, *, run_id: str = "1") -> HistorySample:
    """Return one valid history sample for persistence tests."""
    return HistorySample(
        commit="0" * 40,
        run_id=run_id,
        benchmark_profile_version=BENCHMARK_PROFILE_VERSION,
        worker_iterations=WORKER_ITERATIONS,
        ratios={SCENARIO: ratio},
    )


def _history(*ratios: float) -> BaselineHistory:
    """Return ordered history with the given ratios."""
    return BaselineHistory(
        samples=tuple(
            _sample(ratio, run_id=str(index)) for index, ratio in enumerate(ratios)
        )
    )


def test_history_round_trips_through_json(tmp_path: pth.Path) -> None:
    """Payload conversion and durable JSON preserve the immutable history."""
    history = _history(*TYPICAL_RATIOS)
    assert history_from_payload(history_to_payload(history)) == history, (
        "payload conversion must preserve every history sample"
    )

    path = tmp_path / "main-baseline-history.json"
    write_history(history=history, output_path=path)
    assert load_history(path) == history, "written history must round-trip exactly"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("not json at all", "could not read"),
        ('["not", "an", "object"]', "must contain a JSON object"),
        ('{"schema": 99, "samples": []}', "schema must be"),
    ],
)
def test_an_unusable_history_raises_a_typed_error(
    tmp_path: pth.Path, content: str, reason: str
) -> None:
    """Corrupt persisted state is distinct from an intentionally absent file."""
    path = tmp_path / "main-baseline-history.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BaselineHistoryReadError, match=reason) as error:
        load_history(path)
    assert error.value.path == path, "the typed error must retain its source path"
    assert reason in error.value.reason, "the typed error must retain its reason"


def test_a_malformed_sample_is_an_error_not_a_silent_drop() -> None:
    """A recognized schema with a broken sample is not read as empty."""
    payload = json.loads(
        """{
        "schema": 1,
        "samples": [{
            "commit": "abc",
            "benchmark_profile_version": "profile",
            "worker_iterations": 1,
            "ratios": {"medium-single-nocb": 1.0}
        }]
        }"""
    )
    with pytest.raises(TypeError, match=r"samples\[0\]\.run_id"):
        history_from_payload(payload)


def test_a_missing_history_has_a_distinct_typed_error(tmp_path: pth.Path) -> None:
    """An absent optional artefact has a distinct error."""
    path = tmp_path / "main-baseline-history.json"
    with pytest.raises(BaselineHistoryNotFoundError) as error:
        load_history(path)
    assert error.value.path == path, "the not-found error must retain its path"
    assert error.value.reason == "does not exist", (
        "the not-found error must expose its stable reason"
    )


def test_a_failed_history_replacement_preserves_the_previous_file(
    tmp_path: pth.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed publication cannot truncate the last complete history."""
    path = tmp_path / "main-baseline-history.json"
    existing = _history(1.0)
    write_history(history=existing, output_path=path)

    class _ReplaceError(OSError):
        """A simulated failure replacing the published history."""

    def _replace_fails(source: pth.Path, destination: pth.Path) -> None:
        """Simulate a failure after the temporary history is fully written."""
        del source, destination
        raise _ReplaceError

    monkeypatch.setattr(type(path), "replace", _replace_fails)
    with pytest.raises(_ReplaceError):
        write_history(history=_history(2.0), output_path=path)
    assert load_history(path) == existing, (
        "a failed replacement must preserve the last complete history"
    )
    assert not list(tmp_path.glob(".main-baseline-history.json.*")), (
        "a failed replacement must remove its unpublished temporary history"
    )


def test_a_failed_history_sync_removes_the_temporary_file(
    tmp_path: pth.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write-stage failure leaves no temporary history file."""
    path = tmp_path / "main-baseline-history.json"

    def _sync_fails(_: int) -> None:
        """Simulate storage refusing to persist the temporary payload."""
        message = "simulated fsync failure"
        raise OSError(message)

    monkeypatch.setattr("benchmarks.ratchet_history_persistence.os.fsync", _sync_fails)
    with pytest.raises(OSError, match="simulated fsync failure"):
        write_history(history=_history(2.0), output_path=path)
    assert not list(tmp_path.glob(".main-baseline-history.json.*")), (
        "a failed temporary-file sync must remove the unpublished history file"
    )
