"""Tests for the offline layout comparison and its predeclared decision rule.

The rule decides whether a runner change ships, so the tests take both
directions: they show what the rule adopts and what it refuses, and they show
that a sparse or confounded sample is reported rather than averaged into a
number nobody should act on. The negative cases matter most, because a decision
tool that only ever says "adopted" is indistinguishable from one that does not
look at its input.
"""

from __future__ import annotations

import dataclasses
import json
import typing as typ

import pytest

from scripts.pytest_layout_analysis import (
    EvidenceError,
    fidelity,
    load_records,
    write_reports,
)
from scripts.pytest_layout_evidence import (
    MINIMUM_PAIRS,
    AdoptionThresholds,
    RunRecord,
    compare,
    pair_runs,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


@dataclasses.dataclass(frozen=True, slots=True)
class _Spec:
    """The optional facts about a synthetic run.

    Attributes
    ----------
    seconds : float or None
        Step wall time, or ``None`` when the run recorded none.
    nodes : tuple of str
        Collected node IDs.
    failed : bool
        Whether the run failed or was cancelled.
    """

    seconds: float | None = 100.0
    nodes: tuple[str, ...] = ("node",)
    failed: bool = False
    runner: str | None = "runner-a"
    toolchain: str | None = "toolchain-a"


def _run(run_id: str, layout: str, spec: _Spec | None = None) -> RunRecord:
    """Build one run record for the tests."""
    facts = _Spec() if spec is None else spec
    return RunRecord(
        run_id=run_id,
        layout=layout,
        python_version="3.12",
        cache_state="warm",
        step_seconds=facts.seconds,
        job_seconds=None if facts.seconds is None else facts.seconds * 4,
        node_ids=facts.nodes,
        exit_status=1 if facts.failed else 0,
        failed=facts.failed,
        outcomes=tuple((node, "passed") for node in facts.nodes),
        runner_label=facts.runner,
        toolchain=facts.toolchain,
    )


def _pairs(count: int, saving: float) -> list[RunRecord]:
    """Return ``count`` matched control/candidate pairs with a fixed saving."""
    records: list[RunRecord] = []
    for index in range(count):
        records.extend((
            _run(f"c{index}", "baseline", _Spec(seconds=100.0)),
            _run(f"n{index}", "consolidated", _Spec(seconds=100.0 - saving)),
        ))
    return records


def test_a_clear_saving_is_adopted() -> None:
    """Adopt a candidate whose median saving clears both predeclared minima."""
    comparison = compare(_pairs(MINIMUM_PAIRS, 40.0))

    assert comparison.state == "adopted", comparison.reason
    assert comparison.median_saving_seconds == pytest.approx(40.0), (
        "Adopt a candidate whose median saving clears both predeclared minima"
    )
    assert comparison.median_saving_fraction == pytest.approx(0.4), (
        "Adopt a candidate whose median saving clears both predeclared minima"
    )


def test_too_few_pairs_is_inconclusive() -> None:
    """Refuse to quote a median from a sample smaller than the target."""
    comparison = compare(_pairs(MINIMUM_PAIRS - 1, 40.0))

    assert comparison.state == "inconclusive", (
        "Refuse to quote a median from a sample smaller than the target"
    )
    assert str(MINIMUM_PAIRS - 1) in comparison.reason, (
        "Refuse to quote a median from a sample smaller than the target"
    )


def test_a_saving_below_the_fraction_threshold_is_rejected() -> None:
    """Reject a saving that is large in seconds but small against the job."""
    records: list[RunRecord] = []
    for index in range(MINIMUM_PAIRS):
        records.extend((
            _run(f"c{index}", "baseline", _Spec(seconds=2000.0)),
            _run(f"n{index}", "consolidated", _Spec(seconds=1979.0)),
        ))

    comparison = compare(records)

    assert comparison.median_saving_seconds == pytest.approx(21.0), (
        "Reject a saving that is large in seconds but small against the job"
    )
    assert comparison.state == "rejected", (
        "21s clears the 20s minimum but is 1.05% of a 2000s control, under the "
        "1.5% predeclared minimum, so the fraction must decide"
    )


def test_a_saving_below_the_seconds_threshold_is_rejected() -> None:
    """Reject a saving that is a large fraction of a very short job."""
    records: list[RunRecord] = []
    for index in range(MINIMUM_PAIRS):
        records.extend((
            _run(f"c{index}", "baseline", _Spec(seconds=10.0)),
            _run(f"n{index}", "consolidated", _Spec(seconds=8.5)),
        ))
    comparison = compare(records)

    assert comparison.state == "rejected", (
        "1.5s is 15% of a 10s control but far under the 20s minimum"
    )


def test_one_large_regression_rejects_the_arm() -> None:
    """Refuse a candidate that is usually faster but once much slower."""
    records = _pairs(MINIMUM_PAIRS, 60.0)
    records.append(_run("c-slow", "baseline", _Spec(seconds=100.0)))
    records.append(_run("n-slow", "consolidated", _Spec(seconds=140.0)))

    comparison = compare(records)

    assert comparison.state == "rejected", (
        "Refuse a candidate that is usually faster but once much slower"
    )
    assert "regressed" in comparison.reason, (
        "Refuse a candidate that is usually faster but once much slower"
    )


def test_runs_from_different_interpreters_are_never_paired() -> None:
    """Refuse to pool interpreters, which run different suites."""
    records = [
        _run("c", "baseline"),
        dataclasses.replace(_run("n", "consolidated"), python_version="3.14"),
    ]

    pairs, unpaired = pair_runs(records)

    assert not pairs, "an interpreter mismatch must not produce a pair"
    assert len(unpaired) == 2, "both runs must be reported as unpaired"


def test_runs_from_different_cache_states_are_never_paired() -> None:
    """Refuse to compare a warm build with a cold one."""
    records = [
        _run("c", "baseline"),
        dataclasses.replace(_run("n", "consolidated"), cache_state="cold"),
    ]

    pairs, unpaired = pair_runs(records)

    assert not pairs, "Refuse to compare a warm build with a cold one"
    assert len(unpaired) == 2, "Refuse to compare a warm build with a cold one"


def test_a_failed_run_is_counted_in_the_cost() -> None:
    """Mark a pair whose run failed, so a fast failure is not read as a saving."""
    records = _pairs(MINIMUM_PAIRS, 40.0)
    records[-1] = _run(
        f"n{MINIMUM_PAIRS - 1}",
        "consolidated",
        _Spec(seconds=10.0, failed=True),
    )

    comparison = compare(records)
    report = comparison.as_report()
    pairs = typ.cast("list[dict[str, object]]", report["pairs"])

    assert any(pair["counted_in_cost"] for pair in pairs), (
        "a failed run must be visible in the cost accounting"
    )


def test_a_pair_without_step_times_cannot_supply_a_saving() -> None:
    """Report `inconclusive` rather than a zero saving when times are absent."""
    records = [
        _run(f"c{index}", "baseline", _Spec(seconds=None))
        for index in range(MINIMUM_PAIRS)
    ] + [
        _run(f"n{index}", "consolidated", _Spec(seconds=None))
        for index in range(MINIMUM_PAIRS)
    ]

    comparison = compare(records)

    assert comparison.state == "inconclusive", (
        "Report `inconclusive` rather than a zero saving when times are absent"
    )
    assert "usable pairs" in comparison.reason, (
        "Report `inconclusive` rather than a zero saving when times are absent"
    )


def test_thresholds_can_be_overridden_only_explicitly() -> None:
    """Show the rule is data, so a different experiment can declare its own."""
    comparison = compare(
        _pairs(MINIMUM_PAIRS, 5.0),
        AdoptionThresholds(minimum_saving_seconds=1.0, minimum_saving_fraction=0.01),
    )

    assert comparison.state == "adopted", comparison.reason


def test_loading_refuses_a_missing_file(tmp_path: pth.Path) -> None:
    """Name the path that is absent instead of returning an empty comparison."""
    with pytest.raises(EvidenceError, match="does not exist"):
        load_records(tmp_path / "absent.json")


def test_loading_refuses_invalid_json(tmp_path: pth.Path) -> None:
    """Refuse a truncated or malformed artefact rather than guessing."""
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(EvidenceError, match="not valid JSON"):
        load_records(path)


def test_loading_refuses_an_entry_missing_a_pairing_key(tmp_path: pth.Path) -> None:
    """Refuse a run without the keys that make pairing possible."""
    path = tmp_path / "runs.json"
    path.write_text(json.dumps({"runs": [{"run_id": "a"}]}), encoding="utf-8")

    with pytest.raises(EvidenceError, match="is missing"):
        load_records(path)


def test_loading_accepts_a_single_run_and_a_list(tmp_path: pth.Path) -> None:
    """Accept both shapes the analysis consumes."""
    single = tmp_path / "single.json"
    single.write_text(
        json.dumps({
            "run_id": "a",
            "layout": "baseline",
            "python_version": "3.12",
            "cache_state": "warm",
        }),
        encoding="utf-8",
    )
    listed = tmp_path / "listed.json"
    listed.write_text(
        json.dumps([json.loads(single.read_text(encoding="utf-8"))]), encoding="utf-8"
    )

    assert len(load_records(single)) == 1, "Accept both shapes the analysis consumes"
    assert len(load_records(listed)) == 1, "Accept both shapes the analysis consumes"


def test_fidelity_reports_both_directions() -> None:
    """List the lost and added nodes separately, never as a single count."""
    control = (_run("c", "baseline", _Spec(nodes=("a", "b", "d"))),)
    candidate = (_run("n", "consolidated", _Spec(nodes=("a", "b", "c"))),)

    report = fidelity(control, candidate)

    assert report["control_only"] == ["d"], (
        "List the lost and added nodes separately, never as a single count"
    )
    assert report["candidate_only"] == ["c"], (
        "List the lost and added nodes separately, never as a single count"
    )
    assert report["shared"] == 2, (
        "List the lost and added nodes separately, never as a single count"
    )
    assert report["identical"] is False, (
        "List the lost and added nodes separately, never as a single count"
    )


def test_fidelity_accepts_an_identical_suite() -> None:
    """Report identity when the layouts collected the same nodes."""
    control = (_run("c", "baseline", _Spec(nodes=("a", "b"))),)
    candidate = (_run("n", "consolidated", _Spec(nodes=("b", "a"))),)

    report = fidelity(control, candidate)

    assert report["identical"] is True, (
        "Report identity when the layouts collected the same nodes"
    )
    assert report["control_only"] == [], (
        "Report identity when the layouts collected the same nodes"
    )
    assert report["candidate_only"] == [], (
        "Report identity when the layouts collected the same nodes"
    )


def test_fidelity_detects_a_node_collected_twice() -> None:
    """Detect duplicate collection, which a set comparison would erase.

    Two runs that collect the same node ID twice and once respectively have
    identical sets but different multisets, so the multiset is what proves the
    layouts collected the same suite.
    """
    control = (
        _run("c1", "baseline", _Spec(nodes=("a", "a"))),
        _run("c2", "baseline", _Spec(nodes=("b",))),
    )
    candidate = (
        _run("n1", "consolidated", _Spec(nodes=("a",))),
        _run("n2", "consolidated", _Spec(nodes=("b",))),
    )

    report = fidelity(control, candidate)

    assert report["identical"] is False, (
        "a node collected twice must not compare equal to one collected once"
    )
    assert report["control_duplicates"] == ["a"], (
        f"the repeated node must be reported, got {report['control_duplicates']}"
    )
    assert report["control_total"] == 3, (
        f"multiplicity must be counted, got {report['control_total']}"
    )
    assert report["candidate_total"] == 2, (
        f"the candidate collected two nodes once each, got {report['candidate_total']}"
    )


def test_fidelity_refuses_an_empty_side() -> None:
    """Refuse a comparison against nothing, which would look like a clean diff."""
    populated = (_run("c", "baseline", _Spec(nodes=("a",))),)
    empty = (_run("n", "consolidated", _Spec(nodes=())),)

    with pytest.raises(EvidenceError, match="collected no node IDs"):
        fidelity(populated, empty)


def test_reports_are_written_as_json_and_csv(tmp_path: pth.Path) -> None:
    """Persist both shapes, so a reviewer can read the JSON and diff the CSV."""
    write_reports(tmp_path, {"state": "adopted", "median_saving_seconds": 40.0})

    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    rows = (tmp_path / "report.csv").read_text(encoding="utf-8").splitlines()

    assert payload["state"] == "adopted", (
        "Persist both shapes, so a reviewer can read the JSON and diff the CSV"
    )
    assert rows[0] == "field,value", (
        "Persist both shapes, so a reviewer can read the JSON and diff the CSV"
    )
    assert "state,adopted" in rows, (
        "Persist both shapes, so a reviewer can read the JSON and diff the CSV"
    )
