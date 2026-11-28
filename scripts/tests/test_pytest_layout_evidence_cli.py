"""Tests for the offline evidence command line.

The analysis modules expose functions; a reviewer needs commands. These tests
drive `fidelity` and `compare` as a caller would, against real files, and check
the two statuses that matter: success, and unusable evidence reported as its
own failure rather than as success with an empty report.
"""

from __future__ import annotations

import json
import typing as typ

from scripts.pytest_layout_evidence_cli import main

if typ.TYPE_CHECKING:
    import pathlib as pth


def _write(tmp_path: pth.Path, name: str, nodes: tuple[str, ...]) -> pth.Path:
    """Write one artefact holding a single run."""
    path = tmp_path / name
    path.write_text(
        json.dumps({
            "runs": [
                {
                    "run_id": name,
                    "layout": "baseline" if "control" in name else "consolidated",
                    "python_version": "3.12",
                    "cache_state": "warm",
                    "node_ids": list(nodes),
                }
            ]
        }),
        encoding="utf-8",
    )
    return path


def test_the_cli_writes_a_fidelity_report(tmp_path: pth.Path) -> None:
    """Prove the promised `fidelity` subcommand exists and persists a report."""
    control = _write(tmp_path, "control.json", ("a", "b"))
    candidate = _write(tmp_path, "candidate.json", ("a", "b"))
    output = tmp_path / "out"

    status = main([
        "fidelity",
        "--control",
        str(control),
        "--candidate",
        str(candidate),
        "--output",
        str(output),
    ])

    assert status == 0, f"the command must succeed, got {status}"
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["identical"] is True, f"identical inputs must compare equal: {report}"


def test_the_cli_refuses_an_unusable_artefact(tmp_path: pth.Path) -> None:
    """Report unusable evidence as a distinct status, not as success."""
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    status = main([
        "compare",
        "--input",
        str(broken),
        "--output",
        str(tmp_path / "out"),
    ])

    assert status == 1, f"unusable evidence must exit 1, got {status}"


def test_a_session_record_becomes_a_run_record() -> None:
    """Translate a produced session document into a run record.

    The plugin and the loader were written against different shapes. This is
    the adapter contract: a node collected twice survives as two entries, and
    the outcome, skip and xfail mappings reach the record.
    """
    from scripts.pytest_layout_session_adapter import (
        SessionIdentity,
        run_record_from_session,
    )

    session = {
        "collected": ["a", "a", "b"],
        "outcomes": {"a": "passed", "b": "skipped"},
        "skip_reasons": {"b": "deliberate"},
        "xfail_reasons": {},
        "exit_status": 0,
    }

    record = run_record_from_session(
        session,
        SessionIdentity(
            run_id="r1",
            layout="baseline",
            python_version="3.12",
            cache_state="warm",
            step_seconds=100.0,
            job_seconds=400.0,
            runner_label="runner-a",
            toolchain="toolchain-a",
        ),
    )

    assert record.node_ids == ("a", "a", "b"), (
        f"multiplicity must survive the adapter, got {record.node_ids}"
    )
    assert record.outcomes == (("a", "passed"), ("b", "skipped")), (
        f"outcomes must be carried, got {record.outcomes}"
    )
    assert record.skip_reasons == (("b", "deliberate"),), (
        f"skip reasons must be carried, got {record.skip_reasons}"
    )
    assert record.runner_label == "runner-a", (
        f"the runner fingerprint must be carried, got {record.runner_label!r}"
    )


def _session_document(outcome: str) -> dict[str, object]:
    """Return one produced session document whose first node has ``outcome``."""
    return {
        "collected": ["a", "b"],
        "outcomes": {"a": outcome, "b": "passed"},
        "skip_reasons": {},
        "xfail_reasons": {},
        "exit_status": 0,
    }


def _run_artefact(tmp_path: pth.Path, name: str, layout: str, outcome: str) -> pth.Path:
    """Write one run-record artefact adapted from a produced session document."""
    from scripts.pytest_layout_session_adapter import (
        SessionIdentity,
        run_record_from_session,
    )

    record = run_record_from_session(
        _session_document(outcome),
        SessionIdentity(
            run_id=name,
            layout=layout,
            python_version="3.12",
            cache_state="warm",
            step_seconds=100.0,
            job_seconds=400.0,
            runner_label="runner-a",
            toolchain="toolchain-a",
        ),
    )
    path = tmp_path / f"{name}.json"
    path.write_text(
        json.dumps({
            "runs": [
                {
                    "run_id": record.run_id,
                    "layout": record.layout,
                    "python_version": record.python_version,
                    "cache_state": record.cache_state,
                    "step_seconds": record.step_seconds,
                    "job_seconds": record.job_seconds,
                    "node_ids": list(record.node_ids),
                    "exit_status": record.exit_status,
                    "failed": record.failed,
                    "outcomes": dict(record.outcomes),
                    "skip_reasons": dict(record.skip_reasons),
                    "xfail_reasons": dict(record.xfail_reasons),
                    "runner_label": record.runner_label,
                    "toolchain": record.toolchain,
                }
            ]
        }),
        encoding="utf-8",
    )
    return path


def test_a_produced_session_record_reaches_the_cli(tmp_path: pth.Path) -> None:
    """Carry a produced session document through the adapter into the CLI.

    The node names match on both sides, so only the differing outcomes can make
    the report non-identical, which is exactly the defect the review named.
    """
    control = _run_artefact(tmp_path, "control", "baseline", "passed")
    candidate = _run_artefact(tmp_path, "candidate", "consolidated", "failed")
    output = tmp_path / "out"

    status = main([
        "fidelity",
        "--control",
        str(control),
        "--candidate",
        str(candidate),
        "--output",
        str(output),
    ])

    assert status == 0, f"the command must succeed, got {status}"
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["identical"] is False, (
        f"matching node names with different outcomes must not be identical: {report}"
    )
    assert report["control_total"] == 2, (
        f"both nodes must survive the adapter, got {report['control_total']}"
    )
